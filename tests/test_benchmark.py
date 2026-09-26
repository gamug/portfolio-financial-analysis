"""The internal equal-weight benchmark (``bench-v2``, T-108) and external series loading."""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import write_universe_db
from portfolio_common.db import Database

from kg_schema import connect
from pricing_agent import db as pricing_db
from quant.benchmark import (
    INTERNAL_EW,
    BenchmarkCsvError,
    build_internal_benchmark,
    load_benchmark_csv,
    read_benchmark_csv,
)
from quant.cli import main as quant_main
from quant.config import QuantSettings
from quant.evaluate import PERF_ENGINE_VERSION, run_evaluate
from quant.persist import run_optimize
from quant.returns import run_build_returns

DAYS = ["2025-03-03", "2025-03-04", "2025-03-05", "2025-03-06", "2025-03-07"]
# Daily simple total returns. Asset 3 has no row on 03-05; asset 9 is outside the panel.
SIMPLE: dict[int, list[float | None]] = {
    1: [0.10, -0.05, 0.02, 0.031, -0.012],
    2: [-0.10, 0.04, -0.03, 0.007, 0.025],
    3: [0.03, 0.01, None, -0.022, 0.004],
    9: [5.00, 5.00, 5.00, 5.00, 5.00],
}
PANEL = [1, 2, 3]


def _returns(conn: Database, simple: dict[int, list[float | None]]) -> None:
    for aid, series in simple.items():
        conn.execute("INSERT OR IGNORE INTO assets (id, ticker) VALUES (?, ?)", (aid, f"A{aid}"))
        for day, r in zip(DAYS, series, strict=True):
            if r is None:
                continue
            conn.execute(
                "INSERT INTO quant_return_daily (asset_id, obs_date, close_split_adj, adj_close, "
                "tr_index, tr_log_return, source, engine_version, computed_at) "
                "VALUES (?, ?, 1, 1, 1, ?, 'test', 'qret-v2', 'now')",
                (aid, day, math.log1p(r)),
            )
    conn.commit()


def _series(conn: Database, version: str = "bench-v2") -> list[tuple[str, float, float]]:
    return [
        (str(r["obs_date"]), float(r["log_return"]), float(r["total_return_level"]))
        for r in conn.execute(
            "SELECT obs_date, log_return, total_return_level FROM benchmark_series "
            "WHERE benchmark = ? AND engine_version = ? ORDER BY obs_date",
            (INTERNAL_EW, version),
        )
    ]


def _independent_equal_weight(panel: list[int]) -> list[tuple[str, float]]:
    """An independent calculation: hold 1/n of the value in each panel name that trades that
    day, rebalance daily; the index is the running product of (1 + average simple return)."""
    level, out = 1.0, []
    for i, day in enumerate(DAYS):
        todays = [SIMPLE[a][i] for a in panel if SIMPLE[a][i] is not None]
        value_in, value_out = 0.0, 0.0
        for r in todays:
            stake = level / len(todays)
            value_in += stake
            value_out += stake * (1.0 + float(r or 0.0))
        level *= value_out / value_in
        out.append((day, level))
    return out


def test_the_index_matches_an_independent_equal_weight_calculation(
    memory_quant_db: Database,
) -> None:
    """The T-108 acceptance: to 1e-9, day by day, level and return."""
    _returns(memory_quant_db, SIMPLE)
    n = build_internal_benchmark(
        memory_quant_db, asset_ids=PANEL, date_from=DAYS[0], date_to=DAYS[-1]
    )
    assert n == len(DAYS)
    got = _series(memory_quant_db)
    want = _independent_equal_weight(PANEL)
    prev = 1.0
    for (day, log_return, level), (want_day, want_level) in zip(got, want, strict=True):
        assert day == want_day
        assert level == pytest.approx(want_level, abs=1e-9, rel=0)
        assert math.expm1(log_return) == pytest.approx(want_level / prev - 1.0, abs=1e-9, rel=0)
        prev = want_level


def test_a_name_missing_a_day_is_left_out_of_that_day_not_counted_as_zero(
    memory_quant_db: Database,
) -> None:
    _returns(memory_quant_db, SIMPLE)
    build_internal_benchmark(memory_quant_db, asset_ids=PANEL, date_from=DAYS[0], date_to=DAYS[-1])
    day3 = _series(memory_quant_db)[2]
    assert math.expm1(day3[1]) == pytest.approx((0.02 - 0.03) / 2, abs=1e-12)


def test_names_outside_the_gated_panel_are_ignored(memory_quant_db: Database) -> None:
    """bench-v1 averaged every name with a row; asset 9 (+500% a day) would dominate."""
    _returns(memory_quant_db, SIMPLE)
    build_internal_benchmark(memory_quant_db, asset_ids=PANEL, date_from=DAYS[0], date_to=DAYS[-1])
    assert _series(memory_quant_db)[-1][2] < 1.2


def test_offsetting_returns_leave_an_equal_weight_index_flat(memory_quant_db: Database) -> None:
    """+10% and -10% every day: an equal-weight book rebalanced daily earns exactly 0%. The
    mean of *log* returns (bench-v1) loses about half the variance a day: -0.50%/day here."""
    _returns(memory_quant_db, {1: [0.10] * 5, 2: [-0.10] * 5})
    build_internal_benchmark(memory_quant_db, asset_ids=[1, 2], date_from=DAYS[0], date_to=DAYS[-1])
    assert [level for *_, level in _series(memory_quant_db)] == pytest.approx([1.0] * 5, abs=1e-12)
    mean_of_logs = (math.log(1.10) + math.log(0.90)) / 2
    assert math.exp(5 * mean_of_logs) == pytest.approx(0.9752, abs=1e-4)  # what v1 reported


def test_an_empty_panel_is_refused(memory_quant_db: Database) -> None:
    with pytest.raises(ValueError, match="panel is empty"):
        build_internal_benchmark(memory_quant_db, asset_ids=[], date_from=DAYS[0], date_to=DAYS[-1])


# -- external series --------------------------------------------------------------------------


def _csv(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "spy_tr.csv"
    path.write_text(body)
    return path


def test_an_external_series_loads_with_log_returns_from_its_levels(
    memory_quant_db: Database, tmp_path: Path
) -> None:
    path = _csv(
        tmp_path, "date,total_return_level\n2025-03-03,100\n2025-03-04,101\n2025-03-05,99.99\n"
    )
    assert load_benchmark_csv(memory_quant_db, path, benchmark="SPY_TR") == 3
    rows = memory_quant_db.execute(
        "SELECT obs_date, total_return_level, log_return, source, engine_version "
        "FROM benchmark_series WHERE benchmark = 'SPY_TR' ORDER BY obs_date"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("2025-03-03", 100.0, None, "csv:spy_tr.csv", "csv-v1"),
        ("2025-03-04", 101.0, pytest.approx(math.log(1.01)), "csv:spy_tr.csv", "csv-v1"),
        ("2025-03-05", 99.99, pytest.approx(math.log(99.99 / 101)), "csv:spy_tr.csv", "csv-v1"),
    ]


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("day,level\n2025-03-03,100\n", "columns"),
        ("date,total_return_level\n", "no rows"),
        ("date,total_return_level\n2025-03-04,100\n2025-03-03,101\n", "does not follow"),
        ("date,total_return_level\n2025-03-03,100\n2025-03-03,101\n", "does not follow"),
        ("date,total_return_level\n2025-03-03,0\n", "not positive"),
        ("date,total_return_level\n2025-03-03,nan\n", "not positive"),
        ("date,total_return_level\n03/03/2025,100\n", "line 2"),
        ("date,total_return_level\n2025-03-03,abc\n", "line 2"),
    ],
)
def test_a_malformed_csv_is_refused(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(BenchmarkCsvError, match=match):
        read_benchmark_csv(_csv(tmp_path, body))


def test_the_internal_series_cannot_be_overwritten_by_a_load(
    memory_quant_db: Database, tmp_path: Path
) -> None:
    path = _csv(tmp_path, "date,total_return_level\n2025-03-03,100\n")
    with pytest.raises(ValueError, match="synthesized"):
        load_benchmark_csv(memory_quant_db, path, benchmark=INTERNAL_EW)


# -- evaluate ---------------------------------------------------------------------------------


def _settings(**over: object) -> QuantSettings:
    base: dict[str, object] = {
        "db_path": Path(":memory:"),
        "lookback_days": 200,
        "min_history_days": 140,
        "liquidity_min_dollar_volume": 0.0,
        "max_name_weight": None,
        "max_sector_weight": None,
        "objectives": ["min_var"],
    }
    base.update(over)
    return QuantSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def booked(
    memory_quant_db: Database, quant_seed: Callable[..., Database]
) -> tuple[Database, str, str]:
    """Six seeded names with total returns and a min-variance book 25 sessions before the end;
    a seventh name has too little history for the gate."""
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=280, with_dividends=True)
    run_build_returns(
        QuantSettings(db_path=Path(":memory:")),
        date_from="2000-01-01",
        date_to="2100-01-01",
        conn=conn,
    )
    dates = [
        str(r[0])
        for r in conn.execute("SELECT DISTINCT obs_date FROM quant_return_daily ORDER BY obs_date")
    ]
    conn.execute("INSERT INTO assets (id, ticker) VALUES (7, 'NEW')")
    # NEW is an index member too -- the gate must drop it for its short history alone
    udb = Path(os.environ["KG_UNIVERSE_DB"])
    udb.unlink()
    write_universe_db(
        udb,
        [(f"AS{a:02d}", dates[0], None) for a in range(1, 7)] + [("NEW", dates[0], None)],
    )
    for day in dates[-30:]:  # 30 sessions: far short of the gate's 140
        conn.execute(
            "INSERT INTO quant_return_daily (asset_id, obs_date, close_split_adj, adj_close, "
            "tr_index, tr_log_return, source, engine_version, computed_at) "
            "VALUES (7, ?, 1, 1, 1, 0.5, 'test', 'qret-v2', 'now')",
            (day,),
        )
    conn.commit()
    as_of, end = dates[-25], dates[-1]
    run_optimize(_settings(), as_of=as_of, conn=conn)
    return conn, as_of, end


def test_evaluate_grades_books_against_the_gated_panel_under_new_versions(
    booked: tuple[Database, str, str],
) -> None:
    conn, as_of, end = booked
    # a stale bench-v1 series the view would prefer (newer ingested_at): evaluate must grade
    # against the version it builds, not whatever the view shows
    conn.execute(
        "INSERT INTO benchmark_series (benchmark, obs_date, level, total_return_level, "
        "log_return, source, engine_version, ingested_at) SELECT ?, obs_date, 1, 1, 0.25, "
        "'old', 'bench-v1', '2100-01-01T00:00:00Z' FROM quant_return_daily "
        "WHERE asset_id = 1 AND obs_date > ?",
        (INTERNAL_EW, as_of),
    )
    ev = run_evaluate(_settings(), date_from=as_of, date_to=end, conn=conn)
    assert ev.benchmark_version == "bench-v2"
    assert 7 not in ev.panel  # short history: not in the gate, so not in the benchmark
    assert ev.panel == [1, 2, 3, 4, 5, 6]
    params = json.loads(
        conn.execute(
            "SELECT params_json FROM quant_run WHERE command = 'evaluate' ORDER BY id DESC"
        ).fetchone()[0]
    )
    assert (params["benchmark_version"], params["benchmark_panel"]) == ("bench-v2", ev.panel)
    versions = {
        r[0] for r in conn.execute("SELECT engine_version FROM quant_benchmark_performance")
    }
    assert versions == {PERF_ENGINE_VERSION} == {"perf-v2"}
    # every active return is the book's return minus bench-v2's that day
    bench = {d: math.expm1(lr) for d, lr, _ in _series(conn)}
    for r in conn.execute(
        "SELECT date, realized_return, benchmark_return, active_return "
        "FROM quant_benchmark_performance"
    ):
        assert r["benchmark_return"] == pytest.approx(bench[r["date"]], abs=1e-12)
        assert r["active_return"] == pytest.approx(r["realized_return"] - bench[r["date"]])


def test_evaluate_reads_a_loaded_external_series_and_never_overwrites_it(
    booked: tuple[Database, str, str], tmp_path: Path
) -> None:
    conn, as_of, end = booked
    dates = [
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT obs_date FROM quant_return_daily WHERE obs_date >= ? "
            "ORDER BY obs_date",
            (as_of,),
        )
    ]
    body = "date,total_return_level\n" + "".join(
        f"{d},{100 * 1.001**i}\n" for i, d in enumerate(dates)
    )
    load_benchmark_csv(conn, _csv(tmp_path, body), benchmark="SPY_TR")
    before = conn.execute("SELECT COUNT(*), SUM(log_return) FROM benchmark_series").fetchone()
    ev = run_evaluate(_settings(), date_from=as_of, date_to=end, conn=conn, benchmark="SPY_TR")
    assert (ev.benchmark_rows, ev.benchmark_version, ev.panel) == (0, "csv-v1", [])
    assert tuple(
        conn.execute("SELECT COUNT(*), SUM(log_return) FROM benchmark_series").fetchone()
    ) == tuple(before)
    returns = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT round(benchmark_return, 9) FROM "
            "quant_benchmark_performance WHERE benchmark = 'SPY_TR'"
        )
    }
    assert returns == {0.001}


def test_evaluate_refuses_an_external_series_that_is_not_loaded(
    booked: tuple[Database, str, str],
) -> None:
    conn, as_of, end = booked
    with pytest.raises(ValueError, match="load-benchmark"):
        run_evaluate(_settings(), date_from=as_of, date_to=end, conn=conn, benchmark="SPY_TR")
    status = conn.execute(
        "SELECT status FROM quant_run WHERE command = 'evaluate' ORDER BY id DESC"
    ).fetchone()[0]
    assert status == "failed"


# -- the commands -----------------------------------------------------------------------------


def test_the_benchmark_command_refuses_an_empty_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "kg.db"
    monkeypatch.setenv("KG_FINANCIAL_DB", str(db))
    conn = connect(db)
    pricing_db.ensure_schema(conn)  # assets, prices: what quant's database always has
    conn.execute("INSERT INTO assets (id, ticker) VALUES (1, 'AAA')")  # a member, no returns
    conn.commit()
    conn.close()
    udb = tmp_path / "universe.db"
    write_universe_db(udb, [("AAA", "2020-01-01", None)])
    monkeypatch.setenv("KG_UNIVERSE_DB", str(udb))
    code = quant_main(["benchmark", "--from", "2025-01-02", "--analysis-date", "2025-06-30"])
    assert code == 1
    assert "gate is empty as of 2025-01-02" in capsys.readouterr().err


def test_the_load_benchmark_command_loads_and_reports_bad_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("KG_FINANCIAL_DB", str(tmp_path / "kg.db"))
    good = _csv(tmp_path, "date,total_return_level\n2025-03-03,100\n2025-03-04,101\n")
    assert quant_main(["load-benchmark", "--csv", str(good), "--benchmark", "SPY_TR"]) == 0
    assert "2 rows" in capsys.readouterr().out
    bad = tmp_path / "bad.csv"
    bad.write_text("date,total_return_level\n2025-03-04,100\n2025-03-03,101\n")
    assert quant_main(["load-benchmark", "--csv", str(bad), "--benchmark", "SPY_TR"]) == 1
    assert "does not follow" in capsys.readouterr().err


def test_the_performance_view_shows_one_row_per_book_and_day(
    booked: tuple[Database, str, str],
) -> None:
    """perf-v1 rows (graded against bench-v1) stay stored; the view shows the newest."""
    conn, as_of, end = booked
    run_evaluate(_settings(), date_from=as_of, date_to=end, conn=conn)
    conn.execute(
        "INSERT INTO quant_benchmark_performance (portfolio_id, date, realized_return, "
        "cumulative_return, benchmark, benchmark_return, active_return, engine_version, "
        "computed_at) SELECT portfolio_id, date, realized_return, cumulative_return, benchmark, "
        "benchmark_return - 0.01, active_return + 0.01, 'perf-v1', '2000-01-01T00:00:00Z' "
        "FROM quant_benchmark_performance"
    )
    conn.commit()
    stored = conn.execute("SELECT COUNT(*) FROM quant_benchmark_performance").fetchone()[0]
    shown = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT portfolio_id || date), MIN(engine_version) "
        "FROM v_quant_benchmark_performance"
    ).fetchone()
    assert (shown[0], shown[1], shown[2]) == (stored // 2, stored // 2, "perf-v2")
