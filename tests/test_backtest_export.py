"""The export command: aggregates only, deterministic, and grouped main-first.

A tiny run copy is built in the test with the m018 tables (``kg_schema.ensure``) and two runs
(a ``rule_*`` and a ``w6_*``) so the grouping and the "no per-asset row" contract are exercised.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from cycle.backtest import export
from kg_schema import connect, ensure
from kg_schema.db import connect_ro


def _sessions(start: str, count: int) -> list[str]:
    day = date.fromisoformat(start)
    out: list[str] = []
    while len(out) < count:
        if day.weekday() < 5:
            out.append(day.isoformat())
        day += timedelta(days=1)
    return out


def _run_copy(path: Path, name: str, window: str, *, window_id: int) -> Path:
    conn = connect(path)
    ensure(conn, run_migrations=True)
    sessions = _sessions("2022-01-03", 40)
    conn.execute(
        "INSERT INTO backtest_run (id, window_id, d_start, d_end, formation_date, warmup_steps, "
        "params_json, ips_json, code_version, input_db_sha256, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, 12, ?, '{}', 'abc123', 'sha-input', 'completed', 'now')",
        (
            window_id,
            window,
            sessions[0],
            sessions[-1],
            sessions[0],
            json.dumps(
                {
                    "params_sha256": "sha-params",
                    "warmup_refused": [
                        {"decision_date": "2021-12-01", "reason": "TooManyUnscored"}
                    ],
                    "decisions_refused": [],
                }
            ),
        ),
    )
    books = {}
    for arm in ("MARKOWITZ_BASE", "TOPN_MV_NOGUARD", "TOPN_MV_GUARD_NO_NLP"):
        cursor = conn.execute(
            "INSERT INTO strategy_book (backtest_run_id, arm_code, objective, "
            "exclude_hard_vetoed, guard_layers) VALUES (?, ?, 'min_var', 0, NULL)",
            (window_id, arm),
        )
        books[arm] = int(cursor.lastrowid or 0)
    nav = 1.0
    for index, session in enumerate(sessions):
        bench = 0.001 * (1 if index % 2 else -1)
        for book_id in books.values():
            net = 0.0005 * (1 if index % 3 else -1)
            nav *= 1.0 + net
            conn.execute(
                "INSERT INTO book_daily_performance (book_id, date, gross_return, turnover, "
                "cost_return, net_return, nav, drawdown, cash_weight, n_equity, benchmark_return) "
                "VALUES (?, ?, ?, 0.0, 0.0, ?, ?, 0.0, 0.0, 30, ?)",
                (book_id, session, net, net, nav, bench),
            )
    for book_id in books.values():
        conn.execute(
            "INSERT INTO backtest_summary (book_id, segment, date_from, date_to, "
            "cumulative_return, annualized_return, annualized_vol, ex_post_sharpe, max_drawdown, "
            "total_turnover, total_cost_bps, n_rebalances, n_guard_actions) "
            "VALUES (?, ?, ?, ?, 0.02, 0.1, 0.15, 0.6, 0.05, 0.3, 12.0, 2, 3)",
            (book_id, window, sessions[0], sessions[-1]),
        )
    conn.execute(
        "INSERT INTO backtest_test (backtest_run_id, segment, arm_a, arm_b, test, statistic, "
        "p_value, n_obs, params_json) VALUES (?, ?, 'TOPN_MV_GUARD_NO_NLP', 'TOPN_MV_NOGUARD', "
        "'memmel_jk', 1.25, 0.21, 40, '{}')",
        (window_id, window),
    )
    conn.commit()
    conn.close()
    return path


def test_export_writes_aggregates_grouped_main_first(tmp_path: Path) -> None:
    rule = _run_copy(tmp_path / "rule_P1.db", "rule_P1", "P1", window_id=1)
    w6 = _run_copy(tmp_path / "w6_P1.db", "w6_P1", "P1", window_id=2)
    payload, markdown = export.build([rule, w6])

    assert [run["name"] for run in payload["runs"]] == ["rule_P1", "w6_P1"]
    run = payload["runs"][0]
    assert run["formation_date"] == "2022-01-03"
    assert run["params_sha256"] == "sha-params"
    assert run["input_db_sha256"] == "sha-input"
    assert run["code_version"] == "abc123"
    assert run["warmup_refused"][0]["reason"] == "TooManyUnscored"
    segment = run["segments"]["P1"]
    assert set(segment["arms"]) == {"MARKOWITZ_BASE", "TOPN_MV_NOGUARD", "TOPN_MV_GUARD_NO_NLP"}
    assert segment["arms"]["MARKOWITZ_BASE"]["n_rebalances"] == 2
    assert segment["benchmark"] is not None
    assert segment["benchmark"]["cumulative_return"] is not None
    assert run["tests"][0]["test"] == "memmel_jk"
    assert run["tests"][0]["n_obs"] == 40

    # Main run (rule-based) before the W = 6 group; no per-asset row anywhere.
    assert markdown.index("## Main run") < markdown.index("## Warm-up W = 6")
    assert "TOPN_MV_GUARD_NO_NLP" in markdown
    assert "SP500_EW_INTERNAL" in markdown
    lowered = markdown.lower()
    assert "ticker" not in lowered
    assert "asset_id" not in lowered


def test_export_is_deterministic(tmp_path: Path) -> None:
    rule = _run_copy(tmp_path / "rule_P1.db", "rule_P1", "P1", window_id=1)
    first_payload, first_md = export.build([rule])
    second_payload, second_md = export.build([rule])
    assert json.dumps(first_payload, sort_keys=True) == json.dumps(second_payload, sort_keys=True)
    assert first_md == second_md


def test_export_refuses_a_database_without_a_completed_run(tmp_path: Path) -> None:
    empty = tmp_path / "empty.db"
    conn = connect(empty)
    ensure(conn, run_migrations=True)
    conn.commit()
    conn.close()
    with pytest.raises(export.ExportError, match="no backtest_run"):
        export.read_run(empty)


def test_export_reads_nothing_but_the_contract_tables(tmp_path: Path) -> None:
    """The read set is the five tables of section 9.1 -- a numeric column elsewhere is not read."""
    rule = _run_copy(tmp_path / "rule_P1.db", "rule_P1", "P1", window_id=1)
    conn = connect_ro(rule)
    tables = {
        str(r["name"])
        for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    conn.close()
    assert {"backtest_run", "strategy_book", "backtest_summary", "backtest_test"} <= tables
    payload, _md = export.build([rule])
    assert payload["runs"][0]["name"] == "rule_P1"


def test_the_group_of_a_run_comes_from_its_file_name() -> None:
    assert export.run_group("rule_P1") == "rule"
    assert export.run_group("llm_P3") == "llm"
    assert export.run_group("w6_P2") == "w6"
    assert export.run_group("excl_P1") == "excl"
    assert export.run_group("anything") == "other"
