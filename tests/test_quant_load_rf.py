"""``load-rf``: French's daily RF into ``risk_free_rate`` (BACKTEST_SPEC §8.2, WP-B).

One row per session under ``curve = 'FF_RF_DAILY'`` with ``annualized_rate`` = the daily fraction
x 252, ``source = 'french-ff3-daily'`` and ``engine_version = 'rf-v2'``. The load is idempotent
and refuses a file that is not the one the vendored manifest pins. An unknown ``rf_source``
raises instead of silently falling back to the constant.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from portfolio_common.db import Database

from quant.cli import _run_load_rf, build_parser
from quant.config import QuantSettings
from quant.rates import (
    FF_RF_CURVE,
    FF_RF_ENGINE_VERSION,
    FF_RF_SOURCE,
    RfLoadReport,
    RiskFreeDataError,
    load_french_rf,
    load_risk_free,
)

_CSV = """This file was created by using the 202608 CRSP database.
The Tbill return is the simple daily rate that, over the number of trading days
compounds to 1-month TBill rate.
20240102,   -0.33,   -0.02,   -0.39,    0.01
20240103,    0.10,    0.05,    0.20,    0.02
20240104,    0.20,    0.01,    0.10,    0.021
20240105,   -0.10,    0.00,    0.00,   -99.99

Copyright 2026 Eugene F. Fama and Kenneth R. French
"""


def _write_dataset(root: Path, text: str = _CSV) -> Path:
    (root / "F-F_Research_Data_Factors_daily.csv").write_text(text)
    digest = hashlib.sha256(text.encode()).hexdigest()
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "library_version": "test-v1",
                "files": {"ff3": {"path": "F-F_Research_Data_Factors_daily.csv", "sha256": digest}},
            }
        )
    )
    return root


def _rows(conn: Database) -> list[tuple[str, float, str, str]]:
    return [
        (
            str(r["rate_date"]),
            float(r["annualized_rate"]),
            str(r["source"]),
            str(r["engine_version"]),
        )
        for r in conn.execute(
            "SELECT rate_date, annualized_rate, source, engine_version FROM risk_free_rate "
            "WHERE curve = ? ORDER BY rate_date",
            (FF_RF_CURVE,),
        )
    ]


def test_it_loads_the_daily_french_rf_as_an_annualized_flat_rate(
    memory_quant_db: Database, tmp_path: Path
) -> None:
    rep = load_french_rf(memory_quant_db, data_dir=_write_dataset(tmp_path))

    assert (rep.curve, rep.first_date, rep.last_date) == (FF_RF_CURVE, "2024-01-02", "2024-01-04")
    assert rep.n_rows == 3  # the -99.99 sentinel row is skipped
    assert rep.n_written == 3
    assert rep.library_version == "test-v1"
    rows = _rows(memory_quant_db)
    assert [r[0] for r in rows] == ["2024-01-02", "2024-01-03", "2024-01-04"]
    assert rows[0][1] == pytest.approx(0.01 / 100.0 * 252.0)
    assert rows[1][1] == pytest.approx(0.02 / 100.0 * 252.0)
    assert {r[2] for r in rows} == {FF_RF_SOURCE}
    assert {r[3] for r in rows} == {FF_RF_ENGINE_VERSION}


def test_it_is_idempotent(memory_quant_db: Database, tmp_path: Path) -> None:
    root = _write_dataset(tmp_path)
    first = load_french_rf(memory_quant_db, data_dir=root)
    before = memory_quant_db.execute(
        "SELECT COUNT(*), MAX(ingested_at) FROM risk_free_rate WHERE curve = ?", (FF_RF_CURVE,)
    ).fetchone()
    again = load_french_rf(memory_quant_db, data_dir=root)
    after = memory_quant_db.execute(
        "SELECT COUNT(*), MAX(ingested_at) FROM risk_free_rate WHERE curve = ?", (FF_RF_CURVE,)
    ).fetchone()

    assert (first.n_rows, again.n_rows) == (3, 3)
    assert again.n_written == 0  # unchanged input: no row is touched
    assert tuple(before) == tuple(after)


def test_a_changed_value_updates_the_row_in_place(
    memory_quant_db: Database, tmp_path: Path
) -> None:
    load_french_rf(memory_quant_db, data_dir=_write_dataset(tmp_path))
    changed = _CSV.replace(
        "20240103,    0.10,    0.05,    0.20,    0.02",
        "20240103,    0.10,    0.05,    0.20,    0.03",
    )
    rep = load_french_rf(memory_quant_db, data_dir=_write_dataset(tmp_path, changed))
    assert rep.n_written == 1
    assert len(_rows(memory_quant_db)) == 3  # still one row per session
    assert _rows(memory_quant_db)[1][1] == pytest.approx(0.03 / 100.0 * 252.0)


def test_it_refuses_a_file_that_is_not_the_pinned_one(
    memory_quant_db: Database, tmp_path: Path
) -> None:
    root = _write_dataset(tmp_path)
    (root / "F-F_Research_Data_Factors_daily.csv").write_text(_CSV + "\n20240108, 1, 1, 1, 0.05\n")
    with pytest.raises(RiskFreeDataError, match=r"SHA-256 .* does not match"):
        load_french_rf(memory_quant_db, data_dir=root)


def test_it_refuses_an_unusable_manifest(memory_quant_db: Database, tmp_path: Path) -> None:
    (tmp_path / "F-F_Research_Data_Factors_daily.csv").write_text(_CSV)
    with pytest.raises(RiskFreeDataError, match=r"manifest .* unusable"):
        load_french_rf(memory_quant_db, data_dir=tmp_path)


def test_the_vendored_file_loads(memory_quant_db: Database) -> None:
    """The real pinned file: 1926-07-01 .. 2026-08-31, one row per session."""
    rep = load_french_rf(memory_quant_db)
    assert (rep.first_date, rep.last_date) == ("1926-07-01", "2026-08-31")
    assert rep.n_rows == 26317
    assert rep.n_written == 26317
    last = memory_quant_db.execute(
        "SELECT annualized_rate FROM risk_free_rate WHERE curve = ? AND rate_date = ?",
        (FF_RF_CURVE, "2026-08-31"),
    ).fetchone()[0]
    assert 0.0 < last < 1.0


def test_an_unknown_rf_source_raises_instead_of_using_the_constant(
    memory_quant_db: Database,
) -> None:
    settings = QuantSettings(db_path=Path(":memory:"), rf_source="french-ish")  # a typo
    with pytest.raises(ValueError, match=r"unknown rf_source 'french-ish'.*constant, csv"):
        load_risk_free(settings, as_of="2024-01-05", conn=memory_quant_db)
    assert memory_quant_db.execute("SELECT COUNT(*) FROM risk_free_rate").fetchone()[0] == 0


def test_the_cli_prints_the_curve_summary(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert build_parser().parse_args(["load-rf"]).command == "load-rf"

    def fake(_conn: Database) -> RfLoadReport:
        return RfLoadReport(
            FF_RF_CURVE, "1926-07-01", "2026-08-31", 26317, 26317, "202608 CRSP", "x"
        )

    monkeypatch.setattr("quant.cli.load_french_rf", fake)
    assert _run_load_rf(QuantSettings(db_path=Path(":memory:"))) == 0
    out = capsys.readouterr().out
    assert f"load-rf [{FF_RF_CURVE}, {FF_RF_ENGINE_VERSION}]: 26317 sessions" in out
    assert "2026-08-31" in out and "26317 inserted/changed" in out
