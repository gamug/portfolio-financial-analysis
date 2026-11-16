"""The private Tiingo import (T-153 part 3d): every check, both directions of rule 2, the run
record and idempotence. Hermetic: a synthetic CSV and manifest in ``tmp_path``, never a row from
the private file.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

import pytest
from conftest import cik_for, write_universe_db
from portfolio_common.db import Database

from kg_schema import connect, corporate_actions
from pricing_agent import db as pricing_db
from pricing_agent import pending, pipeline, tiingo
from pricing_agent.config import Settings

ANALYSIS = "2026-10-17"
COLUMNS = [
    "ask_ticker",
    "cik",
    "tiingo_symbol",
    "date",
    "adj_open",
    "adj_high",
    "adj_low",
    "adj_close",
    "adj_volume",
    "raw_close",
    "raw_volume",
    "divCash",
    "splitFactor",
]


def _bar(day: str, close: float, *, dividend: float = 0.0, split: float = 0.0) -> dict[str, str]:
    return {
        "date": day,
        "adj_open": str(close),
        "adj_high": str(close),
        "adj_low": str(close),
        "adj_close": str(close),
        "adj_volume": "1000",
        "raw_close": str(close),
        "raw_volume": "1000",
        "divCash": str(dividend) if dividend else "",
        "splitFactor": str(split) if split else "",
    }


def _write(
    tmp_path: Path,
    lines: dict[tuple[str, str], list[dict[str, str]]],
    *,
    sha256: str | None = None,
    name: str = "leaver_prices",
) -> tuple[Path, Path]:
    """Write a synthetic data file + manifest, returning ``(csv, manifest)``."""
    csv_path = tmp_path / f"{name}.csv"
    rows = []
    manifest_lines = []
    for (ask, cik), bars in lines.items():
        dates = [bar["date"] for bar in bars]
        for bar in bars:
            rows.append({"ask_ticker": ask, "cik": cik, "tiingo_symbol": ask, **bar})
        manifest_lines.append(
            {
                "ask": ask,
                "cik": cik,
                "tiingo_symbol": ask,
                "rows_kept": len(bars),
                "expected_nyse_sessions": len(bars),
                "first_date": min(dates),
                "last_date": max(dates),
                "membership_span": [],
                "notes": [],
                "why_tiingo": {"gateway_candle_count": 0, "production_stored_count": 0},
                "dividend_rows": sum(1 for bar in bars if bar["divCash"]),
                "split_rows": sum(1 for bar in bars if bar["splitFactor"]),
            }
        )
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    digest = sha256 or hashlib.sha256(csv_path.read_bytes()).hexdigest()
    manifest_path = tmp_path / f"{name}.manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "data_file": csv_path.name,
                "data_file_sha256": digest,
                "lines": manifest_lines,
                "not_delivered": [{"ask": "INFO"}, {"ask": "WRK"}],
            }
        ),
        encoding="utf-8",
    )
    return csv_path, manifest_path


def _settings(tmp_path: Path, universe: Path, db: Path) -> Settings:
    return Settings(db_path=db, universe_db_path=universe)


@pytest.fixture
def db(tmp_path: Path) -> Database:
    """A scratch analysis database at the shape the collectors create (assets + shared DDL)."""
    conn = connect(tmp_path / "financial.db")
    pricing_db.ensure_schema(conn)
    return conn


def _asset(conn: Database, ticker: str, cik: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO assets (ticker, cik) VALUES (?, ?)", (ticker, cik or cik_for(ticker))
    )
    conn.commit()
    return int(cur.lastrowid or 0)


LINES = {("AAA", cik_for("AAA")): [_bar("2026-09-01", 10.0), _bar("2026-09-02", 11.0)]}


def _universe(tmp_path: Path, members: list[tuple[str, str, str | None]]) -> Path:
    return write_universe_db(tmp_path / "universe.db", members)


# -- the checks, before anything is written ---------------------------------------------------


def test_a_data_file_inside_a_git_work_tree_is_refused(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    csv_path, manifest = _write(tmp_path, LINES)
    with pytest.raises(tiingo.TiingoFileInWorkTree):
        tiingo.import_tiingo(
            Settings(db_path=tmp_path / "f.db", universe_db_path=tmp_path / "u.db"),
            data_file=csv_path,
            manifest_path=manifest,
            analysis_date=ANALYSIS,
        )
    assert not (tmp_path / "f.db").exists()  # refused before the database was even opened


def test_a_file_whose_sha256_differs_from_the_manifest_is_refused(
    tmp_path: Path, db: Database
) -> None:
    csv_path, manifest = _write(tmp_path, LINES, sha256="0" * 64)
    universe = _universe(tmp_path, [("AAA", "2020-01-01", "2026-09-02")])
    with pytest.raises(tiingo.TiingoHashMismatch):
        tiingo.import_tiingo(
            _settings(tmp_path, universe, tmp_path / "f.db"),
            data_file=csv_path,
            manifest_path=manifest,
            analysis_date=ANALYSIS,
            conn=db,
        )
    assert db.execute("SELECT COUNT(*) AS n FROM price_daily").fetchone()["n"] == 0


def test_a_line_the_manifest_does_not_list_is_refused(tmp_path: Path) -> None:
    csv_path, manifest = _write(tmp_path, LINES)
    data = json.loads(manifest.read_text())
    data["lines"] = []
    manifest.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(tiingo.TiingoLineNotInManifest):
        tiingo.read_lines(csv_path, tiingo.read_manifest(manifest))


def test_a_line_with_no_asset_row_is_refused_by_name(tmp_path: Path, db: Database) -> None:
    csv_path, manifest = _write(tmp_path, LINES)
    universe = _universe(tmp_path, [("AAA", "2020-01-01", "2026-09-02")])
    with pytest.raises(tiingo.TiingoLineHasNoAsset) as excinfo:
        tiingo.import_tiingo(
            _settings(tmp_path, universe, tmp_path / "f.db"),
            data_file=csv_path,
            manifest_path=manifest,
            analysis_date=ANALYSIS,
            conn=db,
        )
    assert "AAA" in str(excinfo.value)


def test_two_lines_of_one_cik_resolve_to_two_assets(tmp_path: Path, db: Database) -> None:
    """DISCA->WBD and DISCK are two lines of CIK 0001437107 (T-153 part 2): each line is its own
    asset, and the manifest's own key keeps them apart."""
    wbd = _asset(db, "WBD", "0001437107")
    disck = _asset(db, "DISCK", "0001437107")
    lines = {
        ("DISCA", "0001437107"): [_bar("2022-04-01", 24.0)],
        ("DISCK", "0001437107"): [_bar("2022-04-01", 25.0)],
    }
    csv_path, manifest = _write(tmp_path, lines)
    universe = _universe(
        tmp_path,
        [("DISCA", "2020-01-01", "2022-04-10"), ("DISCK", "2020-01-01", "2022-04-10")],
    )
    report = tiingo.import_tiingo(
        _settings(tmp_path, universe, tmp_path / "f.db"),
        data_file=csv_path,
        manifest_path=manifest,
        analysis_date=ANALYSIS,
        conn=db,
    )
    assert report.lines_imported == 2
    stored = {
        int(r["asset_id"])
        for r in db.execute("SELECT asset_id FROM price_daily WHERE source = 'tiingo'")
    }
    assert stored == {wbd, disck}


def test_a_row_after_the_lines_last_membership_day_is_refused(tmp_path: Path, db: Database) -> None:
    _asset(db, "AAA")
    csv_path, manifest = _write(tmp_path, LINES)
    universe = _universe(tmp_path, [("AAA", "2020-01-01", "2026-09-01")])
    with pytest.raises(tiingo.TiingoRowAfterMembership) as excinfo:
        tiingo.import_tiingo(
            _settings(tmp_path, universe, tmp_path / "f.db"),
            data_file=csv_path,
            manifest_path=manifest,
            analysis_date=ANALYSIS,
            conn=db,
        )
    assert "2026-09-02" in str(excinfo.value)


def test_a_row_after_the_analysis_date_is_refused(tmp_path: Path, db: Database) -> None:
    _asset(db, "AAA")
    csv_path, manifest = _write(tmp_path, LINES)
    universe = _universe(tmp_path, [("AAA", "2020-01-01", None)])
    with pytest.raises(tiingo.TiingoRowAfterAnalysisDate):
        tiingo.import_tiingo(
            _settings(tmp_path, universe, tmp_path / "f.db"),
            data_file=csv_path,
            manifest_path=manifest,
            analysis_date="2026-09-01",
            conn=db,
        )


def test_a_split_row_the_event_list_does_not_explain_stops_the_import(
    tmp_path: Path, db: Database
) -> None:
    """D10: the class comes from the tracked list, never from a ratio's shape (MKT-02)."""
    _asset(db, "AAA")
    lines = {("AAA", cik_for("AAA")): [_bar("2026-09-01", 10.0, split=1.5)]}
    csv_path, manifest = _write(tmp_path, lines)
    universe = _universe(tmp_path, [("AAA", "2020-01-01", None)])
    with pytest.raises(tiingo.TiingoSplitNotClassified) as excinfo:
        tiingo.import_tiingo(
            _settings(tmp_path, universe, tmp_path / "f.db"),
            data_file=csv_path,
            manifest_path=manifest,
            analysis_date=ANALYSIS,
            conn=db,
        )
    assert "1.5" in str(excinfo.value)
    assert db.execute("SELECT COUNT(*) AS n FROM price_daily").fetchone()["n"] == 0


# -- the write, rule 4 and idempotence --------------------------------------------------------


def test_the_import_writes_the_bars_and_counts_them_on_its_run(
    tmp_path: Path, db: Database
) -> None:
    _asset(db, "AAA")
    csv_path, manifest = _write(tmp_path, LINES)
    universe = _universe(tmp_path, [("AAA", "2020-01-01", None)])
    report = tiingo.import_tiingo(
        _settings(tmp_path, universe, tmp_path / "f.db"),
        data_file=csv_path,
        manifest_path=manifest,
        analysis_date=ANALYSIS,
        conn=db,
    )
    assert (report.price_rows, report.lines_imported, report.lines) == (2, 1, 1)
    rows = db.execute("SELECT date, close, source FROM price_daily ORDER BY date").fetchall()
    assert [(r["date"], r["source"]) for r in rows] == [
        ("2026-09-01", "tiingo"),
        ("2026-09-02", "tiingo"),
    ]
    run = db.execute(
        "SELECT params_json FROM pricing_run WHERE id = ?", (report.run_id,)
    ).fetchone()
    params = json.loads(run["params_json"])
    assert params["kind"] == "tiingo-import"
    assert params["file_sha256"] == hashlib.sha256(csv_path.read_bytes()).hexdigest()
    assert params["price_rows"] == 2 and params["lines_imported"] == 1
    assert params["lines_gateway_only"] == [] and params["lines_gateway_only_count"] == 0
    # the database and the record agree (verify_pilot's T-153f reads exactly this)
    assert (
        db.execute("SELECT COUNT(*) AS n FROM price_daily WHERE source = 'tiingo'").fetchone()["n"]
        == params["price_rows"]
    )


def test_a_second_run_writes_nothing(tmp_path: Path, db: Database) -> None:
    _asset(db, "AAA")
    csv_path, manifest = _write(tmp_path, LINES)
    universe = _universe(tmp_path, [("AAA", "2020-01-01", None)])
    settings = _settings(tmp_path, universe, tmp_path / "f.db")
    first = tiingo.import_tiingo(
        settings,
        data_file=csv_path,
        manifest_path=manifest,
        analysis_date=ANALYSIS,
        conn=db,
    )
    second = tiingo.import_tiingo(
        settings,
        data_file=csv_path,
        manifest_path=manifest,
        analysis_date=ANALYSIS,
        conn=db,
    )
    assert (first.price_rows, second.price_rows) == (2, 0)
    assert second.per_line[0]["price_rows"] == 0
    assert (
        db.execute("SELECT COUNT(*) AS n FROM price_daily WHERE source = 'tiingo'").fetchone()["n"]
        == 2
    )


def test_the_dividend_is_the_split_adjusted_one_and_the_split_is_recorded(
    tmp_path: Path, db: Database
) -> None:
    """The manifest's own convention: `divCash / split_cum`, and a split row under the import's
    engine. The split itself is K's (a listed event)."""
    cik = "0000055067"
    _asset(db, "K", cik)
    lines = {
        ("K", cik): [
            _bar("2023-09-29", 50.0, dividend=0.5),
            _bar("2023-10-02", 50.0 / 1.065, split=1.065),
            _bar("2023-10-03", 47.0),
        ]
    }
    csv_path, manifest = _write(tmp_path, lines)
    universe = _universe(tmp_path, [("K", "2020-01-01", None)])
    report = tiingo.import_tiingo(
        _settings(tmp_path, universe, tmp_path / "f.db"),
        data_file=csv_path,
        manifest_path=manifest,
        analysis_date=ANALYSIS,
        conn=db,
    )
    assert (report.dividend_rows, report.split_rows) == (1, 1)
    actions = db.execute(
        "SELECT action_type, ex_date, value, source, engine_version FROM corporate_action "
        "ORDER BY action_type"
    ).fetchall()
    assert [(r["action_type"], r["source"], r["engine_version"]) for r in actions] == [
        ("DIVIDEND", "tiingo", corporate_actions.TIINGO_ENGINE),
        ("SPLIT", "tiingo", corporate_actions.TIINGO_ENGINE),
    ]
    # the pre-split dividend is brought onto the price basis: 0.5 / 1.065
    assert actions[0]["value"] == pytest.approx(0.5 / 1.065)


# -- rule 2, both directions (A1/A2) ----------------------------------------------------------


def test_a_line_with_any_gateway_bar_is_imported_as_gateway_only(
    tmp_path: Path, db: Database
) -> None:
    """A2/WBD: the gateway serves the whole line, so no Tiingo row is written for it -- not even
    on the dates the file shares with the stored rows -- and the run names it."""
    asset_id = _asset(db, "WBD", "0001437107")
    db.execute(
        "INSERT INTO price_daily (asset_id, date, close, source) VALUES (?, '2026-09-01', 12.0, "
        "'yfinance')",
        (asset_id,),
    )
    db.commit()
    lines = {("WBD", "0001437107"): [_bar("2026-09-01", 10.0), _bar("2026-09-02", 11.0)]}
    csv_path, manifest = _write(tmp_path, lines)
    universe = _universe(tmp_path, [("WBD", "2020-01-01", None)])
    report = tiingo.import_tiingo(
        _settings(tmp_path, universe, tmp_path / "f.db"),
        data_file=csv_path,
        manifest_path=manifest,
        analysis_date=ANALYSIS,
        conn=db,
    )
    assert report.lines_imported == 0
    assert [
        (line.ticker, line.cik, line.asset_id, line.stored_rows) for line in report.gateway_only
    ] == [("WBD", "0001437107", asset_id, 1)]
    run = db.execute(
        "SELECT params_json FROM pricing_run WHERE id = ?", (report.run_id,)
    ).fetchone()
    params = json.loads(run["params_json"])
    assert params["lines_gateway_only"] == [
        {"ticker": "WBD", "cik": "0001437107", "asset_id": asset_id, "stored_rows": 1}
    ]
    assert params["lines_gateway_only_count"] == 1
    # nothing from the file was written, on the shared date or anywhere else
    assert (
        db.execute("SELECT COUNT(*) AS n FROM price_daily WHERE source = 'tiingo'").fetchone()["n"]
        == 0
    )
    # ... and it is never a pricing_run_error row: pending reads only the latest error per ticker
    assert db.execute("SELECT COUNT(*) AS n FROM pricing_run_error").fetchone()["n"] == 0


def test_a_fresh_database_leaves_the_gateway_only_list_empty(tmp_path: Path, db: Database) -> None:
    """Addendum 2: pilot A stops when the list is not empty on a fresh database, so the empty
    case is asserted too."""
    _asset(db, "AAA")
    csv_path, manifest = _write(tmp_path, LINES)
    universe = _universe(tmp_path, [("AAA", "2020-01-01", None)])
    report = tiingo.import_tiingo(
        _settings(tmp_path, universe, tmp_path / "f.db"),
        data_file=csv_path,
        manifest_path=manifest,
        analysis_date=ANALYSIS,
        conn=db,
    )
    assert report.gateway_only == []
    run = db.execute(
        "SELECT params_json FROM pricing_run WHERE id = ?", (report.run_id,)
    ).fetchone()
    assert json.loads(run["params_json"])["lines_gateway_only_count"] == 0


def test_the_gateway_returning_a_line_that_holds_tiingo_rows_is_refused(
    tmp_path: Path, db: Database
) -> None:
    """A1: the run refuses the line, writes nothing for it, records stage 'tiingo_stitch', and
    pending treats that refusal like a split_jump one."""
    asset_id = _asset(db, "AAA")
    db.execute(
        "INSERT INTO price_daily (asset_id, date, close, source) VALUES (?, '2026-09-01', 10.0, "
        "'tiingo')",
        (asset_id,),
    )
    db.commit()
    engine = type(
        "E",
        (),
        {
            "params": pipeline.RunParams(analysis_date=ANALYSIS, store_daily=True),
            "conn": db,
            "report": pipeline.RunReport(run_id=1, planned=1),
        },
    )()
    task = pipeline._Task(ticker="AAA", asset_id=asset_id, start="2026-08-01", end="2026-09-02")
    prices = pipeline.DailyPrices(
        ticker="AAA",
        start_date="2026-08-01",
        end_date="2026-09-02",
        candles=[
            pipeline.Candle(
                date="2026-09-02",
                open=11.0,
                high=11.0,
                low=11.0,
                close=11.0,
                volume=1.0,
                source="yfinance",
            )
        ],
        source="yfinance",
        warning=None,
    )
    with pytest.raises(pipeline._TiingoStitchRefused):
        pipeline._store(engine, task, prices)
    assert db.execute("SELECT COUNT(*) AS n FROM price_daily").fetchone()["n"] == 1
    row = db.execute("SELECT source FROM price_daily WHERE date = '2026-09-02'").fetchone()
    assert row is None
    # pending's own reading of the recorded refusal
    db.execute("INSERT INTO pricing_run (started_at, status) VALUES ('now', 'completed')")
    run_id = int(db.execute("SELECT MAX(id) AS i FROM pricing_run").fetchone()["i"])
    db.execute(
        "INSERT INTO pricing_run_error (run_id, ticker, stage, message, created_at) "
        "VALUES (?, 'AAA', 'tiingo_stitch', 'stitch', 'now')",
        (run_id,),
    )
    db.commit()
    assert pending._refused(db, "AAA") is True
