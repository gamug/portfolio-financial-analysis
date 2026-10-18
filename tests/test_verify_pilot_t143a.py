"""verify_pilot's T-143a check (T-143 part A, P1): a metrics-only run leaves no FUNDAMENTAL row,
and every filing it wrote carries its ``value_inputs`` rule input (the run's own done predicate)."""

from __future__ import annotations

import json
from pathlib import Path

from portfolio_common.db import Database
from verify_pilot import metrics_only_problems

from fundamental_agent.db import ensure_schema
from fundamental_agent.value_inputs import GROUP, NAME
from kg_schema import connect
from kg_schema.trading_calendar import available_from

FILED = "2024-05-01"


def _db(tmp_path: Path) -> tuple[str, Database]:
    path = tmp_path / "financial.db"
    conn = connect(path)
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO assets (ticker, company_name, cik) VALUES ('AAA', 'AAA Inc.', '0000000001')"
    )
    conn.commit()
    return str(path), conn


def _run(conn: Database, *, metrics_only: bool) -> int:
    cur = conn.execute(
        "INSERT INTO analysis_run (started_at, params_json, status) VALUES ('now', ?, 'completed')",
        (json.dumps({"metrics_only": metrics_only}),),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def _filing(
    conn: Database,
    run_id: int,
    accession: str,
    *,
    value_inputs: bool = True,
    score: bool = False,
) -> int:
    asset_id = int(conn.execute("SELECT id FROM assets").fetchone()["id"])
    available = available_from(FILED)
    cur = conn.execute(
        "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, filing_date, "
        "accession_number, period_end, retrieved_at, cik, available_at, run_id) "
        "VALUES (?, '10-Q', 2024, '2024Q1', ?, ?, '2024-03-31', 'now', '0000000001', ?, ?)",
        (asset_id, FILED, accession, available, run_id),
    )
    filing_id = int(cur.lastrowid or 0)
    if value_inputs:
        conn.execute(
            "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
            "inputs_json, computed_at, run_id, available_at) "
            "VALUES (?, ?, ?, 1.0, 'usd', '{}', 'now', ?, ?)",
            (filing_id, GROUP, NAME, run_id, available),
        )
    if score:
        conn.execute(
            "INSERT INTO score_snapshot (asset_id, score_type, event_time, computed_at, filing_id, "
            "run_id, available_at) "
            "VALUES (?, 'FUNDAMENTAL', '2024-03-31', 'now', ?, ?, ?)",
            (asset_id, filing_id, run_id, available),
        )
    conn.commit()
    return filing_id


def test_a_database_with_no_metrics_only_run_passes(tmp_path: Path) -> None:
    path, _conn = _db(tmp_path)
    assert metrics_only_problems(path) == (0, [])


def test_a_clean_metrics_only_run_passes(tmp_path: Path) -> None:
    path, conn = _db(tmp_path)
    run = _run(conn, metrics_only=True)
    _filing(conn, run, "acc-only", value_inputs=True)
    assert metrics_only_problems(path) == (1, [])


def test_a_fundamental_row_from_a_metrics_only_run_fails(tmp_path: Path) -> None:
    path, conn = _db(tmp_path)
    run = _run(conn, metrics_only=True)
    _filing(conn, run, "acc-only", value_inputs=True, score=True)
    runs, problems = metrics_only_problems(path)
    assert runs == 1
    assert any("1 FUNDAMENTAL score_snapshot row(s)" in p for p in problems), problems


def test_a_metrics_only_filing_without_value_inputs_fails(tmp_path: Path) -> None:
    path, conn = _db(tmp_path)
    run = _run(conn, metrics_only=True)
    _filing(conn, run, "acc-stopped", value_inputs=False)
    runs, problems = metrics_only_problems(path)
    assert runs == 1
    assert any("1 filing(s) without a value_inputs row" in p for p in problems), problems


def test_a_normal_run_filing_is_not_judged(tmp_path: Path) -> None:
    """The check only judges metrics-only runs; a normal run's filings (with or without a score)
    are none of its business."""
    path, conn = _db(tmp_path)
    run = _run(conn, metrics_only=False)
    _filing(conn, run, "acc-normal", value_inputs=True, score=True)
    assert metrics_only_problems(path) == (0, [])
