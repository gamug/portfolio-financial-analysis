"""Price ingestion integrity (T-131): closed sessions only, observations over the full stored
history, and no split-shaped jump stored without an explanation."""

from __future__ import annotations

import math
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import write_universe_db

from kg_schema import rundate
from pricing_agent import pipeline
from pricing_agent.config import Settings
from pricing_agent.integrity import (
    Jump,
    is_split_shaped,
    matches_recorded_split,
    split_shaped_jumps,
)
from pricing_agent.observations import build_observations
from pricing_agent.pipeline import RunParams, RunReport, SessionNotClosed
from pricing_agent.pricing_client import Candle, DailyPrices

_FIRST = date(2024, 1, 1)
_OBS_COLUMNS = (
    "close, prev_close, log_return, true_range, atr_14, realized_vol_21d, realized_vol_90d, "
    "max_drawdown_90d, momentum_21d, momentum_63d, momentum_252d, dollar_volume"
)


def _day(i: int) -> str:
    return (_FIRST + timedelta(days=i)).isoformat()


def _base(i: int) -> float:
    """A smooth price path: no day-over-day move anywhere near a split ratio."""
    return 100.0 + 0.05 * i + 3.0 * math.sin(i / 7.0)


def _candle(i: int, close: float, volume: float = 1_000_000.0) -> Candle:
    return Candle(_day(i), close, close * 1.01, close * 0.99, close, volume, "test")


def _series(n: int, *, scale_before: tuple[int, float] | None = None) -> list[Candle]:
    """*n* bars on :func:`_base`; those before index ``scale_before[0]`` multiplied by
    ``scale_before[1]`` -- the pre-split basis of a series the gateway has not re-adjusted."""
    out = []
    for i in range(n):
        factor = scale_before[1] if scale_before and i < scale_before[0] else 1.0
        out.append(_candle(i, _base(i) * factor))
    return out


class _Gateway:
    """A pricing gateway serving a fixed candle list, filtered to the requested range."""

    def __init__(self, candles: list[Candle]) -> None:
        self.candles = candles
        self.calls: list[tuple[str, str]] = []

    def __enter__(self) -> _Gateway:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def daily_any_spelling(self, ticker: str, start: str, end: str) -> DailyPrices:
        self.calls.append((start, end))
        return DailyPrices(
            ticker=ticker,
            start_date=start,
            end_date=end,
            source="test",
            candles=[c for c in self.candles if start <= c.date <= end],
            warning=None,
        )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    udb = write_universe_db(tmp_path / "universe.db", [("AAPL", "2020-01-01", None)])
    return Settings(
        db_path=tmp_path / "kg.db", universe_db_path=udb, pricing_base_url="http://pricing.test"
    )


def _run(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    gateway: _Gateway,
    *,
    start: str,
    end: str,
    **flags: Any,
) -> RunReport:
    monkeypatch.setattr(pipeline, "PricingClient", lambda *_a, **_k: gateway)
    params = RunParams(start_date=start, end_date=end, analysis_date=end, store_daily=True, **flags)
    return pipeline.run(settings, params)


def _db(settings: Settings) -> sqlite3.Connection:
    return sqlite3.connect(settings.db_path)


def _closes(settings: Settings) -> dict[str, float]:
    return dict(_db(settings).execute("SELECT date, close FROM price_daily ORDER BY date"))


# -- (a) a session that has not closed ---------------------------------------------------------


def _clock(monkeypatch: pytest.MonkeyPatch, iso: str) -> None:
    monkeypatch.setattr(rundate, "now", lambda: datetime.fromisoformat(iso).replace(tzinfo=UTC))


def test_a_run_for_a_session_still_open_is_refused_before_anything_is_written(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 2026-09-29 runs at 14:04 UTC (10:04 ET) stored a partial bar as that day's close."""
    _clock(monkeypatch, "2026-09-29T14:04")
    with pytest.raises(SessionNotClosed, match=r"2026-09-29.*2026-09-28"):
        pipeline.run(settings, RunParams(analysis_date="2026-09-29", store_daily=True))
    assert not settings.db_path.exists()


def test_the_open_session_is_refused_through_end_too(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clock(monkeypatch, "2026-09-29T14:04")
    with pytest.raises(SessionNotClosed):
        pipeline.run(settings, RunParams(analysis_date="2026-09-29", end_date="2026-09-29"))


@pytest.mark.parametrize(
    ("now", "end"),
    [
        ("2026-09-29T14:04", "2026-09-28"),  # the previous, closed session
        ("2026-09-29T21:00", "2026-09-29"),  # the same day once the close has settled
        ("2026-10-03T15:00", "2026-10-03"),  # a Saturday has no session
    ],
)
def test_a_closed_or_non_session_end_is_accepted(
    settings: Settings, monkeypatch: pytest.MonkeyPatch, now: str, end: str
) -> None:
    _clock(monkeypatch, now)
    monkeypatch.setattr(pipeline, "PricingClient", lambda *_a, **_k: _Gateway(_series(5)))
    report = pipeline.run(settings, RunParams(analysis_date=end, start_date="2024-01-01"))
    assert report.completed == 1


# -- (b) observations come from the full stored history ----------------------------------------

_END_FIRST, _END_SECOND = _day(279), _day(299)


def _observation_rows(settings: Settings) -> dict[str, tuple[Any, ...]]:
    rows = _db(settings).execute(
        f"SELECT obs_date, {_OBS_COLUMNS} FROM price_observation ORDER BY obs_date"  # noqa: S608
    )
    return {r[0]: tuple(r[1:]) for r in rows}


def _full_recompute(candles: list[Candle]) -> dict[str, tuple[Any, ...]]:
    return {
        o.obs_date: (
            o.close,
            o.prev_close,
            o.log_return,
            o.true_range,
            o.atr_14,
            o.realized_vol_21d,
            o.realized_vol_90d,
            o.max_drawdown_90d,
            o.momentum_21d,
            o.momentum_63d,
            o.momentum_252d,
            o.dollar_volume,
        )
        for o in build_observations(candles, engine_version="x")
    }


def test_an_incremental_refresh_gives_the_observations_of_a_full_recompute(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T-122's refresh (``--start 2026-08-20``) left 90-day vol and 252-day momentum NULL on
    every row it created, because the observations saw only the fetched candles."""
    candles = _series(300)
    gateway = _Gateway(candles)
    _run(settings, monkeypatch, gateway, start=_day(0), end=_END_FIRST, observations=True)
    # a short refresh over the last month, as a daily job would run
    _run(settings, monkeypatch, gateway, start=_day(270), end=_END_SECOND, observations=True)

    stored = _observation_rows(settings)
    assert stored == _full_recompute(candles)
    last = stored[_day(299)]
    assert last[6] is not None  # realized_vol_90d
    assert last[10] is not None  # momentum_252d


def test_a_corrected_bar_reaches_every_observation_that_depends_on_it(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    candles = _series(300)
    _run(
        settings, monkeypatch, _Gateway(candles), start=_day(0), end=_END_SECOND, observations=True
    )

    corrected = [*candles]
    corrected[250] = _candle(250, _base(250) * 1.05)  # a vendor revises one bar
    _run(
        settings,
        monkeypatch,
        _Gateway(corrected),
        start=_day(250),
        end=_END_SECOND,
        observations=True,
    )
    assert _observation_rows(settings) == _full_recompute(corrected)


def test_a_rerun_on_unchanged_prices_rewrites_no_observation(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = _Gateway(_series(300))
    _run(settings, monkeypatch, gateway, start=_day(0), end=_END_SECOND, observations=True)
    conn = _db(settings)
    conn.execute("UPDATE price_observation SET computed_at = 'sentinel'")
    conn.commit()

    _run(settings, monkeypatch, gateway, start=_day(0), end=_END_SECOND, observations=True)
    assert _db(settings).execute(
        "SELECT COUNT(*) FROM price_observation WHERE computed_at != 'sentinel'"
    ).fetchone() == (0,)


# -- (c) splits ----------------------------------------------------------------------------------

_EX = 200  # index of the split's ex-date
_PRE_SPLIT_BASIS = (_EX, 2.0)  # bars before the ex-date sit at 2x until re-adjusted


def _record_split(settings: Settings, ex_index: int, value: float = 2.0) -> None:
    conn = _db(settings)
    conn.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) "
        "SELECT id, 'SPLIT', ?, ?, 'pricing-gateway', 'corpact-v1', '2024-01-01' FROM assets",
        (_day(ex_index), value),
    )
    conn.commit()


def _age_stored_rows(settings: Settings, before_index: int) -> None:
    """Make every stored bar look written before the split (the earlier run that stored them)."""
    conn = _db(settings)
    conn.execute(
        "UPDATE price_daily SET ingested_at = ?", (_day(before_index) + "T00:00:00+00:00",)
    )
    conn.commit()


def _store_pre_split_history(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """Bars 0..189 as the gateway served them before the split: on the old (2x) basis."""
    old = _series(190, scale_before=(1000, 2.0))
    _run(settings, monkeypatch, _Gateway(old), start=_day(0), end=_day(189), observations=True)
    _age_stored_rows(settings, _EX - 5)


def _adjusted_gateway() -> _Gateway:
    """The gateway after the split: the whole history on the new basis, no jump anywhere."""
    return _Gateway(_series(230))


def test_a_split_after_stored_history_re_fetches_the_full_history_once(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APH split 2:1 on 2026-09-03; a refresh from 08-20 left the older bars unadjusted, a fake
    -51% at the seam (the refresh start) and observations built on it."""
    _store_pre_split_history(settings, monkeypatch)
    _record_split(settings, _EX)
    gateway = _adjusted_gateway()

    report = _run(settings, monkeypatch, gateway, start=_day(205), end=_day(229), observations=True)

    assert report.failed == 0
    assert report.full_refetches == ["AAPL"]
    assert gateway.calls == [(_day(205), _day(229)), (_day(0), _day(229))]
    closes = _closes(settings)
    assert closes == {c.date: c.close for c in gateway.candles}
    assert (
        split_shaped_jumps(Candle(d, c, c, c, c, 1.0, "t") for d, c in sorted(closes.items())) == []
    )
    # the observations were rebuilt on the adjusted history, not left on the old basis
    assert _observation_rows(settings) == _full_recompute(gateway.candles)

    # one re-fetch, not one per run: every pre-split bar now carries a post-split stamp
    again = _run(settings, monkeypatch, gateway, start=_day(205), end=_day(229), observations=True)
    assert again.full_refetches == []
    assert gateway.calls[-1] == (_day(205), _day(229))


def test_a_series_with_no_recorded_split_to_explain_a_jump_is_refused(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MNST/APH: a seam jump the table of splits does not (yet) explain is not stored."""
    _store_pre_split_history(settings, monkeypatch)
    before = _closes(settings)

    report = _run(settings, monkeypatch, _adjusted_gateway(), start=_day(190), end=_day(229))

    assert (report.failed, report.completed) == (1, 0)
    assert "backfill-actions" in report.errors[0]
    assert _closes(settings) == before  # nothing written for the ticker
    stage = _db(settings).execute("SELECT stage FROM pricing_run_error").fetchone()
    assert stage == ("split_jump",)


def test_allow_split_jumps_stores_a_series_whose_jump_is_a_real_move(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_pre_split_history(settings, monkeypatch)

    report = _run(
        settings,
        monkeypatch,
        _adjusted_gateway(),
        start=_day(190),
        end=_day(229),
        allow_split_jumps=True,
    )
    assert (report.failed, report.completed) == (0, 1)
    assert _day(229) in _closes(settings)


def test_a_jump_that_survives_the_full_re_fetch_is_refused(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recorded split but a gateway still serving the raw, unadjusted series: the jump is
    at the ex-date inside the fetched window, so re-fetching cannot fix it."""
    _store_pre_split_history(settings, monkeypatch)
    _record_split(settings, _EX)
    raw = _Gateway(_series(230, scale_before=_PRE_SPLIT_BASIS))
    before = _closes(settings)

    report = _run(settings, monkeypatch, raw, start=_day(195), end=_day(229))

    assert report.failed == 1
    assert report.full_refetches == ["AAPL"]
    assert "still returns it unadjusted" in report.errors[0]
    assert _closes(settings) == before


def test_an_old_jump_the_run_does_not_touch_is_not_its_to_refuse(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_old_jump = _series(300)
    with_old_jump[40:] = [_candle(i, _base(i) * 2.0) for i in range(40, 300)]  # a x2 at day 40
    gateway = _Gateway(with_old_jump)
    first = _run(
        settings, monkeypatch, gateway, start=_day(0), end=_day(279), allow_split_jumps=True
    )
    assert first.completed == 1

    later = _run(settings, monkeypatch, gateway, start=_day(270), end=_END_SECOND)
    assert (later.failed, later.completed) == (0, 1)


# -- the detector ------------------------------------------------------------------------------


@pytest.mark.parametrize("ratio", [2.0, 0.5, 3.0, 1 / 3, 4.0, 0.25, 10.0, 0.1, 2.04, 0.49])
def test_split_ratios_are_split_shaped(ratio: float) -> None:
    assert is_split_shaped(ratio)


@pytest.mark.parametrize("ratio", [1.0, 0.56, 0.6, 1.7, 2.8, 1.5, 0.9, 1.1])
def test_real_moves_are_not(ratio: float) -> None:
    """FISV -44% (0.56), CNC -40%, ECHO +70%, MRNA x2.8 are market moves, not seam errors."""
    assert not is_split_shaped(ratio)


def test_a_jump_matches_a_recorded_split_either_way_round() -> None:
    assert matches_recorded_split(Jump("a", "b", 0.5), [2.0])
    assert matches_recorded_split(Jump("a", "b", 2.0), [2.0])  # a reverse seam
    assert not matches_recorded_split(Jump("a", "b", 0.5), [3.0])
    assert not matches_recorded_split(Jump("a", "b", 0.5), [])
