"""WP-G's calibration command: hand-computed values on a tiny fixture, and its two refusals.

The fixture is synthetic (no real data): two assets, five sessions, returns chosen so every
pooled quantile, the EW volatility and the EW drawdown can be written out by hand:

    A: +0.10  -0.20  +0.05  +0.02  -0.01
    B: +0.01  +0.02  +0.03  -0.04  +0.05

With ``J = 2`` (monkeypatched; the frozen value is 21):

* ``S(2)`` pool = A: -0.10, -0.15, +0.07, +0.01; B: +0.03, +0.05, -0.01, +0.01
  sorted: -0.15 -0.10 -0.01 +0.01 +0.01 +0.03 +0.05 +0.07
  ``Q0.05`` (linear, position 0.35) = -0.15 + 0.35 x 0.05 = -0.1325 -> gamma = 0.1325
* one-session pool, sorted: -0.20 -0.04 -0.01 +0.01 +0.02 +0.02 +0.03 +0.05 +0.05 +0.10
  ``Q0.75`` (position 6.75) = 0.03 + 0.75 x 0.02 = 0.045 -> delta = 0.045
* EW portfolio: the return of session *j* is the mean over the members as of session *j-1*,
  so the first session earns nothing and the four returns are -0.09, +0.04, -0.01, +0.02:
  mean -0.01, sum of squares 0.0098, var(ddof=1) 0.0098/3, std 0.0571550,
  x sqrt(252) = 0.9073
* NAV: 0.91, 0.9464, 0.936936, 0.95567472; the trough is 0.91 against a starting 1.0,
  so MDD = 0.09, alpha = 0.91, m = 1/0.09 = 11.1111
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from cycle.backtest import calibrate
from cycle.backtest.calibrate import CalibrationError
from cycle.backtest.driver import load_params
from kg_schema import connect, trading_calendar

UNIVERSE_DDL = """
CREATE TABLE universe_membership (
    id INTEGER PRIMARY KEY,
    symbol TEXT NOT NULL,
    security TEXT NOT NULL,
    cik TEXT,
    gics_sector TEXT,
    gics_sub_industry TEXT,
    hq_location TEXT,
    date_added TEXT,
    founded TEXT,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    detected_at TEXT NOT NULL,
    run_id INTEGER,
    run_kind TEXT,
    source TEXT NOT NULL
);
"""

RETURNS = {
    "AAA": [0.10, -0.20, 0.05, 0.02, -0.01],
    "BBB": [0.01, 0.02, 0.03, -0.04, 0.05],
}


def _now() -> str:
    return datetime.now(tz=None).isoformat(timespec="seconds")


def _sessions(count: int) -> list[str]:
    first = date(2021, 11, 1)
    day = (
        trading_calendar.next_trading_day(first)
        if not trading_calendar.is_trading_day(first)
        else first
    )
    out = [day.isoformat()]
    while len(out) < count:
        day = trading_calendar.next_trading_day(day)
        out.append(day.isoformat())
    return out


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "financial.db"
    conn = connect(db_path)
    conn.create_schema(
        "CREATE TABLE assets (id INTEGER PRIMARY KEY, ticker TEXT, company_name TEXT, cik TEXT);"
    )
    conn.execute(
        "INSERT INTO assets (id, ticker, company_name, cik) VALUES "
        "(1, 'AAA', 'Alpha', '0000000001'), (2, 'BBB', 'Beta', '0000000002')"
    )
    conn.execute(
        "CREATE TABLE quant_return_daily ("
        "id INTEGER PRIMARY KEY, asset_id INTEGER NOT NULL, obs_date TEXT NOT NULL, "
        "close_split_adj REAL, adj_close REAL, tr_index REAL, tr_log_return REAL, "
        "source TEXT, engine_version TEXT, computed_at TEXT)"
    )
    sessions = _sessions(len(RETURNS["AAA"]))
    for asset_id, symbol in ((1, "AAA"), (2, "BBB")):
        for session, value in zip(sessions, RETURNS[symbol], strict=True):
            conn.execute(
                "INSERT INTO quant_return_daily (asset_id, obs_date, tr_log_return, source, "
                "engine_version, computed_at) VALUES (?, ?, ?, 'quant-tr-v1', 'qret-v2', ?)",
                (asset_id, session, float(np.log1p(value)), _now()),
            )
    conn.commit()
    conn.close()

    universe_path = tmp_path / "universe.db"
    universe = connect(universe_path)
    universe.create_schema(UNIVERSE_DDL)
    for cik, symbol in (("0000000001", "AAA"), ("0000000002", "BBB")):
        universe.execute(
            "INSERT INTO universe_membership (symbol, security, cik, valid_from, detected_at, "
            "source) VALUES (?, ?, ?, '2020-01-01', ?, 'test')",
            (symbol, f"{symbol} Inc.", cik, _now()),
        )
    universe.commit()
    universe.close()
    return db_path, universe_path


def _calibrate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> calibrate.Calibration:
    db_path, universe_path = _fixture(tmp_path)
    monkeypatch.setattr(calibrate, "J", 2)
    sessions = _sessions(5)
    return calibrate.calibrate(
        db_path, universe_path, date_from=sessions[0], date_to=sessions[-1], leavers_dir=None
    )


def test_hand_computed_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calib = _calibrate(tmp_path, monkeypatch)
    assert calib.sessions == 5
    assert calib.member_sessions == 10
    assert calib.s_values == 8
    assert calib.delta_values == 10
    assert calib.ew_sessions == 4
    assert calib.gamma == pytest.approx(0.1325, abs=1e-12)
    assert calib.delta == pytest.approx(0.045, abs=1e-12)
    assert calib.sigma_star == pytest.approx(0.9073, abs=5e-5)
    assert calib.alpha == pytest.approx(0.91, abs=1e-12)
    assert calib.m == pytest.approx(11.1112, abs=1e-9)
    assert calib.ew_max_drawdown == pytest.approx(0.09, abs=1e-12)


def test_ew_vol_and_mdd_by_hand(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The two statistics re-derived independently of the module's own helpers."""
    db_path, universe_path = _fixture(tmp_path)
    monkeypatch.setattr(calibrate, "J", 2)
    sessions = _sessions(5)
    ew = np.array([-0.09, 0.04, -0.01, 0.02])
    assert np.std(ew, ddof=1) * np.sqrt(252) == pytest.approx(0.9073, abs=5e-5)
    nav = np.cumprod(1 + ew)
    assert float(1 - nav.min()) == pytest.approx(0.09, abs=1e-12)
    calib = calibrate.calibrate(
        db_path, universe_path, date_from=sessions[0], date_to=sessions[-1], leavers_dir=None
    )
    assert calib.sigma_star == pytest.approx(float(np.std(ew, ddof=1) * np.sqrt(252)), abs=5e-5)


def _leavers_dir(tmp_path: Path, sessions: list[str]) -> Path:
    """A WP-G-style calibration pair for a member the database has no asset row for (L-42)."""
    path = tmp_path / "leavers"
    path.mkdir()
    dates = [_previous_session(sessions[0]), *sessions]
    closes = [100.0, 101.0, 103.0, 102.0, 104.0, 105.0]
    lines = ["cik,symbol,date,open,high,low,close,volume,source"]
    for day, close in zip(dates, closes, strict=True):
        lines.append(f"0000000009,LVR,{day},{close},{close},{close},{close},1000,yfinance")
    (path / "prices.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (path / "actions.csv").write_text("cik,symbol,action_type,ex_date,value\n", encoding="utf-8")
    return path


def _previous_session(session: str) -> str:
    return trading_calendar.last_session_on_or_before(
        date.fromisoformat(session) - timedelta(days=1)
    ).isoformat()


def _add_universe_leaver(universe_path: Path) -> None:
    """One PIT member with no row in ``assets`` -- the L-42 case the calibration files cover."""
    universe = connect(universe_path)
    universe.execute(
        "INSERT INTO universe_membership (symbol, security, cik, valid_from, detected_at, "
        "source) VALUES ('LVR', 'Leaver Inc.', '0000000009', '2020-01-01', ?, 'test')",
        ("2020-01-01",),
    )
    universe.commit()
    universe.close()


def test_a_leaver_with_no_assets_row_enters_the_panel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R-1.1: a PIT member absent from ``assets`` but present in the leaver files is used."""
    db_path, universe_path = _fixture(tmp_path)
    monkeypatch.setattr(calibrate, "J", 2)
    _add_universe_leaver(universe_path)
    sessions = _sessions(5)
    leavers = _leavers_dir(tmp_path, sessions)

    without = calibrate.calibrate(
        db_path, universe_path, date_from=sessions[0], date_to=sessions[-1], leavers_dir=None
    )
    assert without.member_sessions == 10
    assert without.s_values == 8
    assert without.missing_assets_by_year == {sessions[0][:4]: 5}

    with_leavers = calibrate.calibrate(
        db_path,
        universe_path,
        date_from=sessions[0],
        date_to=sessions[-1],
        leavers_dir=leavers,
    )
    # Five more member-sessions (one per session) and four more S(2) windows.
    assert with_leavers.member_sessions == 15
    assert with_leavers.s_values == 12
    assert with_leavers.delta_values == 15
    assert with_leavers.leaver_series == 1
    assert with_leavers.leaver_pairs == 5
    assert with_leavers.ew_sessions == 4
    # After the files, no member-session of the sample is left without a return.
    assert with_leavers.missing_return_by_year == {sessions[0][:4]: 0}
    # The leaver's returns move the calibrated values (they are not silently dropped).
    assert with_leavers.delta != without.delta


def test_refuses_a_range_reaching_p1(tmp_path: Path) -> None:
    db_path, universe_path = _fixture(tmp_path)
    with pytest.raises(CalibrationError, match="never calibrated on P1-P3"):
        calibrate.calibrate(db_path, universe_path, date_from="2021-12-01", date_to="2022-01-03")
    with pytest.raises(CalibrationError, match="never calibrated on P1-P3"):
        calibrate.calibrate(db_path, universe_path, date_from="2019-01-02", date_to="2023-10-27")


def test_refuses_the_production_path(tmp_path: Path) -> None:
    _db_path, universe_path = _fixture(tmp_path)
    with pytest.raises(CalibrationError, match="read-only"):
        calibrate.calibrate(
            "/workspaces/thesis/data/pilot_a3/financial.db",
            universe_path,
            date_from="2021-11-01",
            date_to="2021-11-05",
        )


def test_output_toml_loads_and_builds_guard_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calib = _calibrate(tmp_path, monkeypatch)
    out = tmp_path / "params.frozen.toml"
    out.write_text(calibrate.render_params(calib, unscored_max_share=0.06), encoding="utf-8")
    params = load_params(out)
    assert params.unscored_max_share == pytest.approx(0.06)
    assert params.exclude_tickers == ()
    assert params.shadow_from == "TOPN_MV_GUARD_NO_NLP"
    assert set(params.guard) == {"TOPN_MV_GUARD_NO_NLP"}
    guard = params.guard["TOPN_MV_GUARD_NO_NLP"]
    assert guard.layers == frozenset({1, 3, 4})
    assert guard.news is None
    assert guard.gamma == pytest.approx(0.1325)
    assert guard.delta == pytest.approx(0.045)
    assert guard.sigma_star == pytest.approx(0.9073)
    assert guard.alpha == pytest.approx(0.91)
    assert guard.m == pytest.approx(11.1112)


def test_unscored_max_share_is_optional(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Case A writes no key (CycleSettings' 0.05 stays the default); Case B writes the value."""
    calib = _calibrate(tmp_path, monkeypatch)
    without = tmp_path / "without.toml"
    without.write_text(calibrate.render_params(calib), encoding="utf-8")
    assert load_params(without).unscored_max_share is None
    with_key = tmp_path / "with.toml"
    with_key.write_text(calibrate.render_params(calib, unscored_max_share=0.06), encoding="utf-8")
    assert load_params(with_key).unscored_max_share == pytest.approx(0.06)


def test_exclude_tickers_round_trips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calib = _calibrate(tmp_path, monkeypatch)
    path = tmp_path / "excl.toml"
    text = calibrate.render_params(calib).replace(
        'rf_curve = "FF_RF_DAILY"', 'rf_curve = "FF_RF_DAILY"\nexclude_tickers = ["v", "BRK.B"]'
    )
    path.write_text(text, encoding="utf-8")
    assert load_params(path).exclude_tickers == ("BRK.B", "V")
