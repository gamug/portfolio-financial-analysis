"""Ring-1 deterministic data-quality gates (T-065) and the table they write (T-040).

The gates judge a filing's *stored* metrics; ``cycle`` reads their verdicts: a quarantined
metric reads as NULL, and a HARD verdict raises a DATA_QUALITY veto (T-1 lag, like every
veto). Thresholds are PLAN.md Work item 7's table -- the boundary cases here pin the strict
inequalities it states.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from portfolio_common.db import Database

import kg_schema
from cycle.config import CycleSettings
from cycle.data import data_quality
from cycle.orchestrator import run_selection
from fundamental_agent import db, quality
from fundamental_agent.cli import main as fundamental_main
from fundamental_agent.metrics import compute_group
from fundamental_agent.quality import Issue, StoredMetric, evaluate, gate_all, gate_version
from kg_schema import connect
from kg_schema.trading_calendar import available_from
from kg_schema.versions import MetricVersions

FCF = ("valuation", "free_cash_flow_yield")
NET = ("profitability", "net_margin")
OCF = ("cashflow", "operating_cash_flow_margin")
MCAP = ("valuation", "market_capitalization")
DTE = ("leverage", "debt_to_equity")
DTA = ("leverage", "debt_to_assets")
COV = ("leverage", "interest_coverage")
ROE = ("profitability", "return_on_equity")


def _fm(**values: Any) -> dict[tuple[str, str], StoredMetric]:
    """A clean filing -- no gate fires -- with *values* overriding metric values
    (``net_margin=9``) or statement inputs (``equity=-1``, ``total_assets=...``)."""
    inputs = {
        "revenue": 100.0,
        "net_income": 10.0,
        "equity": 50.0,
        "total_assets": 200.0,
        "operating_cash_flow": 20.0,
    }
    metric_values: dict[tuple[str, str], float | None] = {
        FCF: 0.05,
        NET: 0.10,
        OCF: 0.20,
        MCAP: 400.0,
        DTE: 1.0,
        DTA: 0.3,
        COV: 8.0,
        ROE: 0.2,
        ("profitability", "gross_margin"): 0.4,
        ("valuation", "enterprise_value"): 450.0,
    }
    by_name = {key[1]: key for key in metric_values}
    for name, value in values.items():
        if name in by_name:
            metric_values[by_name[name]] = value
        else:
            inputs[name] = value
    return {key: StoredMetric(v, dict(inputs)) for key, v in metric_values.items()}


def _rules(issues: list[Issue]) -> set[tuple[str, str, bool]]:
    return {(i.rule_id, i.severity, i.quarantined) for i in issues}


# -- the gates, one by one ---------------------------------------------------------------------


def test_a_clean_filing_trips_nothing() -> None:
    assert evaluate(_fm()) == []


def test_the_seven_gates_are_plan_md_s_table() -> None:
    assert quality.RULE_IDS == (
        "DQ_FCF_YIELD",
        "DQ_MARGIN",
        "DQ_MARGIN_REVIEW",
        "DQ_OCF_MARGIN",
        "DQ_MCAP_SCALE",
        "DQ_NEG_EQUITY",
        "DQ_REVENUE_POS",
    )


@pytest.mark.parametrize(
    ("value", "hit"), [(0.51, True), (-0.51, True), (0.50, False), (-0.5, False), (None, False)]
)
def test_fcf_yield(value: float | None, hit: bool) -> None:
    issues = evaluate(_fm(free_cash_flow_yield=value))
    assert _rules(issues) == ({("DQ_FCF_YIELD", "HARD", True)} if hit else set())
    if hit:
        assert [i.metric for i in issues] == [FCF]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5.01, {("DQ_MARGIN", "HARD", True)}),
        (-7.0, {("DQ_MARGIN", "HARD", True)}),
        (5.0, {("DQ_MARGIN_REVIEW", "SOFT", False)}),
        (1.01, {("DQ_MARGIN_REVIEW", "SOFT", False)}),
        (-2.0, {("DQ_MARGIN_REVIEW", "SOFT", False)}),
        (1.0, set()),
        (0.3, set()),
    ],
)
def test_net_margin_hard_above_5_review_in_1_to_5(value: float, expected: set[Any]) -> None:
    assert _rules(evaluate(_fm(net_margin=value))) == expected


@pytest.mark.parametrize(("value", "hit"), [(3.01, True), (-4.0, True), (3.0, False)])
def test_ocf_margin(value: float, hit: bool) -> None:
    assert _rules(evaluate(_fm(operating_cash_flow_margin=value))) == (
        {("DQ_OCF_MARGIN", "HARD", True)} if hit else set()
    )


@pytest.mark.parametrize(
    ("mcap", "hit"),
    [(0.19, True), (20_001.0, True), (0.2, False), (20_000.0, False), (None, False)],
)
def test_mcap_scale_against_total_assets_200(mcap: float | None, hit: bool) -> None:
    issues = evaluate(_fm(market_capitalization=mcap))
    assert _rules(issues) == ({("DQ_MCAP_SCALE", "HARD", True)} if hit else set())
    if hit and mcap is not None:  # every *stored* valuation metric built on market cap goes too
        assert {i.metric for i in issues} == {MCAP, FCF, ("valuation", "enterprise_value")}
        assert issues[0].evidence["ratio"] == pytest.approx(mcap / 200.0)


@pytest.mark.parametrize("assets", [None, 0.0, -5.0])
def test_mcap_scale_needs_positive_total_assets(assets: float | None) -> None:
    assert evaluate(_fm(market_capitalization=1e9, total_assets=assets)) == []


@pytest.mark.parametrize(
    ("dta", "cov", "severity"),
    [
        (0.81, 8.0, "HARD"),
        (0.3, 1.49, "HARD"),
        (0.8, 1.5, "SOFT"),  # both limits are strict
        (None, None, "SOFT"),
    ],
)
def test_negative_equity_quarantines_de_and_roe_hard_only_when_distressed(
    dta: float | None, cov: float | None, severity: str
) -> None:
    issues = evaluate(_fm(equity=-10.0, debt_to_assets=dta, interest_coverage=cov))
    assert _rules(issues) == {("DQ_NEG_EQUITY", severity, True)}
    assert {i.metric for i in issues} == {DTE, ROE}


def test_zero_equity_is_negative_equity_and_positive_or_missing_is_not() -> None:
    assert _rules(evaluate(_fm(equity=0.0))) == {("DQ_NEG_EQUITY", "SOFT", True)}
    assert evaluate(_fm(equity=0.01)) == []
    assert evaluate(_fm(equity=None)) == []


@pytest.mark.parametrize(
    ("revenue", "hit"), [(0.0, True), (-3.0, True), (None, True), (1.0, False)]
)
def test_revenue_must_be_positive_when_net_income_is_reported(
    revenue: float | None, hit: bool
) -> None:
    issues = [i for i in evaluate(_fm(revenue=revenue)) if i.rule_id == "DQ_REVENUE_POS"]
    assert bool(issues) is hit
    if hit:  # the revenue-denominated ratios the filing stored
        assert {i.metric for i in issues} == {NET, OCF, ("profitability", "gross_margin")}
        assert {(i.severity, i.quarantined) for i in issues} == {("HARD", True)}


def test_revenue_gate_is_silent_without_net_income() -> None:
    assert evaluate(_fm(revenue=None, net_income=None)) == []


# -- recording --------------------------------------------------------------------------------


def _filing(conn: Database, asset_id: int, ticker: str, period_end: str = "2025-12-31") -> int:
    conn.execute("INSERT OR IGNORE INTO assets (id, ticker) VALUES (?, ?)", (asset_id, ticker))
    return int(
        conn.execute(
            "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
            "filing_date, available_at, retrieved_at) VALUES (?, '10-K', ?, ?, ?, ?, ?, "
            "'2026-01-01T00:00:00Z') RETURNING id",
            (
                asset_id,
                int(period_end[:4]),
                f"FY{period_end[:4]}",
                period_end,
                filed := (date.fromisoformat(period_end) + timedelta(days=45)).isoformat(),
                available_from(filed),
            ),
        ).fetchone()[0]
    )


def _store(conn: Database, filing_id: int, version: str, **values: Any) -> None:
    for (group, name), stored in _fm(**values).items():
        conn.execute(
            "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, unit, "
            "inputs_json, computed_at, engine_version, event_time, available_at) VALUES "
            "(?, ?, ?, ?, 'x', ?, '2026-01-01T00:00:00Z', ?, '2025-12-31', "
            "(SELECT available_at FROM sec_filings WHERE id = ?))",
            (
                filing_id,
                group,
                name,
                stored.value,
                json.dumps(dict(stored.inputs)),
                version,
                filing_id,
            ),
        )
    conn.commit()


def _issues(conn: Database) -> list[tuple[Any, ...]]:
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT filing_id, metric_name, metric_engine_version, rule_id, severity, "
            "quarantined, gate_version FROM data_quality_issue ORDER BY filing_id, rule_id, "
            "metric_name"
        )
    ]


def test_gating_records_one_row_per_metric_and_is_idempotent(memory_db: Database) -> None:
    conn = memory_db
    fid = _filing(conn, 1, "AAA")
    _store(conn, fid, "metrics-v1", net_margin=9.0)
    first = gate_version(conn, "metrics-v1", run_id=7)
    assert (first.filings, first.inserted) == (1, 1)
    assert first.filings_by_rule == {"DQ_MARGIN": 1} and first.hard_by_rule == {"DQ_MARGIN": 1}
    assert _issues(conn) == [(fid, "net_margin", "metrics-v1", "DQ_MARGIN", "HARD", 1, "dq-v1")]
    row = conn.execute("SELECT asset_id, value, run_id FROM data_quality_issue").fetchone()
    assert (row["asset_id"], row["value"], row["run_id"]) == (1, 9.0, 7)
    again = gate_version(conn, "metrics-v1")
    assert (again.inserted, again.filings_by_rule) == (0, {"DQ_MARGIN": 1})
    assert len(_issues(conn)) == 1


def test_each_metrics_version_is_judged_on_its_own(memory_db: Database) -> None:
    """A fixed engine's parallel rows (metrics-v2) are clean; the v1 verdict stays v1's."""
    conn = memory_db
    kg_schema.apply_migrations(conn)  # the migrated key is what lets versions coexist
    fid = _filing(conn, 1, "AAA")
    _store(conn, fid, "metrics-v1", net_margin=9.0)
    _store(conn, fid, "metrics-v2")
    reports = gate_all(conn)
    assert set(reports) == {"metrics-v1", "metrics-v2"}
    assert reports["metrics-v2"].filings_by_rule == {}
    assert {r[2] for r in _issues(conn)} == {"metrics-v1"}
    assert gate_all(conn, engine_version="metrics-v9") == {}


def test_one_filing_can_be_gated_alone(memory_db: Database) -> None:
    conn = memory_db
    a = _filing(conn, 1, "AAA")
    b = _filing(conn, 2, "BBB")
    _store(conn, a, "metrics-v1", net_margin=9.0)
    _store(conn, b, "metrics-v1", net_margin=9.0)
    assert gate_version(conn, "metrics-v1", filing_id=b).filings == 1
    assert {r[0] for r in _issues(conn)} == {b}


def test_the_table_checks_severity_and_the_view_names_the_filing(memory_db: Database) -> None:
    conn = memory_db
    fid = _filing(conn, 1, "AAA")
    _store(conn, fid, "metrics-v1", equity=-5.0, debt_to_assets=0.9)
    gate_version(conn, "metrics-v1")
    rows = conn.execute(
        "SELECT ticker, form, fiscal_period, metric_name, severity FROM v_data_quality_issue "
        "ORDER BY metric_name"
    ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("AAA", "10-K", "FY2025", "debt_to_equity", "HARD"),
        ("AAA", "10-K", "FY2025", "return_on_equity", "HARD"),
    ]
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO data_quality_issue (filing_id, asset_id, metric_group, metric_name, "
            "metric_engine_version, rule_id, severity, quarantined, gate_version, created_at) "
            "VALUES (?, 1, 'x', 'y', 'metrics-v1', 'DQ_X', 'MEDIUM', 0, 'dq-v1', 'now')",
            (fid,),
        )


def test_the_quality_command_backfills_and_reports(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "fin.db"
    conn = connect(path)
    db.ensure_schema(conn)
    fid = _filing(conn, 1, "AAA")
    _store(conn, fid, "metrics-v1", operating_cash_flow_margin=4.0)
    conn.close()
    assert fundamental_main(["quality", "--db", str(path)]) == 0
    out = capsys.readouterr().out
    assert "metrics-v1: 1 filings gated, 1 new issue rows" in out
    assert "DQ_OCF_MARGIN" in out and "1 filings  (1 HARD)" in out
    assert fundamental_main(["quality", "--db", str(path), "--metrics-version", "metrics-v7"]) == 1


# -- what cycle does with the verdicts -----------------------------------------------------------


V1 = MetricVersions({"profitability": "metrics-v1", "valuation": "metrics-v1"})


def _issue(  # noqa: PLR0913 - one hand-written row
    conn: Database,
    filing_id: int,
    metric: tuple[str, str],
    rule_id: str,
    severity: str,
    *,
    quarantined: int = 1,
    version: str = "metrics-v1",
    gate: str = "dq-v1",
) -> None:
    conn.execute(
        "INSERT INTO data_quality_issue (filing_id, asset_id, metric_group, metric_name, "
        "metric_engine_version, rule_id, severity, quarantined, value, gate_version, created_at) "
        "SELECT id, asset_id, ?, ?, ?, ?, ?, ?, 1.0, ?, 'now' FROM sec_filings WHERE id = ?",
        (metric[0], metric[1], version, rule_id, severity, quarantined, gate, filing_id),
    )
    conn.commit()


def test_cycle_reads_verdicts_on_the_latest_filing_for_its_versions_only(
    memory_db: Database,
) -> None:
    conn = memory_db
    old = _filing(conn, 1, "AAA", "2024-12-31")
    new = _filing(conn, 1, "AAA", "2025-12-31")
    _issue(conn, old, NET, "DQ_MARGIN", "HARD")  # an older filing's verdict
    _issue(conn, new, FCF, "DQ_FCF_YIELD", "HARD")
    _issue(conn, new, NET, "DQ_MARGIN_REVIEW", "SOFT", quarantined=0)  # review only
    _issue(conn, new, MCAP, "DQ_MCAP_SCALE", "HARD", version="metrics-v2")  # other version
    _issue(conn, new, ("valuation", "enterprise_value"), "DQ_MCAP_SCALE", "HARD", gate="dq-v0")
    dq = data_quality(conn, "2026-06-30", V1)
    assert dq.quarantined == {1: {"valuation.free_cash_flow_yield"}}
    assert dq.hard == {
        1: [{"rule_id": "DQ_FCF_YIELD", "metric": "valuation.free_cash_flow_yield", "value": 1.0}]
    }
    # point in time: before the newer filing's period end, the older one is the latest
    assert data_quality(conn, "2025-06-30", V1).quarantined == {1: {"profitability.net_margin"}}
    assert dq.apply({1: {"valuation.free_cash_flow_yield": 0.9, "x.y": 2.0}}) == {
        1: {"valuation.free_cash_flow_yield": None, "x.y": 2.0}
    }


def _cycle_settings() -> CycleSettings:
    return CycleSettings(db_path=Path(":memory:"), top_n=3)


def _ranking(conn: Database, cycle_date: str) -> dict[str, tuple[int, int]]:
    return {
        r["ticker"]: (r["vetoed"], r["selected"])
        for r in conn.execute(
            "SELECT ticker, vetoed, selected FROM v_cycle_ranking WHERE cycle_date = ?",
            (cycle_date,),
        )
    }


def test_a_hard_gate_vetoes_the_asset_from_the_next_cycle(cycle_seed: Database) -> None:
    """End to end through the real gates: AAA (the seed's best-ranked name) reports an
    implausible FCF yield; the gate runs, then cycle raises DATA_QUALITY (T-1 lag)."""
    conn = cycle_seed
    conn.execute(
        "UPDATE fundamental_metrics SET value = 0.9 WHERE filing_id = "
        "(SELECT id FROM sec_filings WHERE asset_id = 1) AND metric_name = 'free_cash_flow_yield'"
    )
    conn.commit()
    assert gate_all(conn)["metrics-v1"].filings_by_rule == {"DQ_FCF_YIELD": 1}

    run_selection(_cycle_settings(), "2026-06-30", conn=conn)
    veto = conn.execute(
        "SELECT severity, evidence_json FROM veto WHERE asset_id = 1 AND rule_id = 'DATA_QUALITY'"
    ).fetchone()
    assert veto["severity"] == "HARD" and '"DQ_FCF_YIELD"' in veto["evidence_json"]
    assert _ranking(conn, "2026-06-30")["AAA"][0] == 0  # same-day exemption

    run_selection(_cycle_settings(), "2026-07-01", conn=conn)
    assert _ranking(conn, "2026-07-01")["AAA"] == (1, 0)
    catalog = conn.execute(
        "SELECT severity FROM rule_catalog WHERE rule_id = 'DATA_QUALITY'"
    ).fetchone()
    assert catalog["severity"] == "HARD"


def _valorization(conn: Database, asset_id: int, cycle_date: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT inputs_json FROM score_snapshot WHERE score_type = 'VALORIZATION' "
        "AND asset_id = ? AND event_time = ?",
        (asset_id, cycle_date),
    ).fetchone()
    return dict(json.loads(row["inputs_json"]))


def test_negative_equity_quarantine_keeps_leverage_the_worst_and_does_not_veto(
    cycle_seed: Database,
) -> None:
    """A SOFT DQ_NEG_EQUITY on AAA (the seed's lowest D/E): D/E and ROE read as NULL, the
    leverage factor still ranks AAA worst (C2), and nothing vetoes it."""
    conn = cycle_seed
    run_selection(_cycle_settings(), "2026-06-29", conn=conn)
    before = _valorization(conn, 1, "2026-06-29")["quality"]
    fid = int(conn.execute("SELECT id FROM sec_filings WHERE asset_id = 1").fetchone()[0])
    _issue(conn, fid, DTE, "DQ_NEG_EQUITY", "SOFT")
    _issue(conn, fid, ROE, "DQ_NEG_EQUITY", "SOFT")

    run_selection(_cycle_settings(), "2026-06-30", conn=conn)
    after = _valorization(conn, 1, "2026-06-30")["quality"]
    assert after < before
    rules = {r[0] for r in conn.execute("SELECT rule_id FROM veto WHERE asset_id = 1")}
    assert "DATA_QUALITY" not in rules and "LEVERAGE_EXTREME" not in rules


def test_a_quarantined_metric_drops_out_of_the_score(cycle_seed: Database) -> None:
    """AAA's value factor rests on its FCF yield alone in the seed; quarantined, it is gone."""
    conn = cycle_seed
    run_selection(_cycle_settings(), "2026-06-29", conn=conn)
    assert _valorization(conn, 1, "2026-06-29")["value"] is not None
    fid = int(conn.execute("SELECT id FROM sec_filings WHERE asset_id = 1").fetchone()[0])
    _issue(conn, fid, FCF, "DQ_FCF_YIELD", "HARD")

    run_selection(_cycle_settings(), "2026-06-30", conn=conn)
    assert _valorization(conn, 1, "2026-06-30")["value"] is None
    assert _valorization(conn, 2, "2026-06-30")["value"] is not None


def test_nee_s_real_10k_now_passes_the_revenue_gate(nee_10k: Any) -> None:
    """T-102 end to end on NEE's captured FY2025 10-K: the metrics the engine computes from it
    now carry a revenue, so DQ_REVENUE_POS (which vetoed NEE in every cycle) stays silent."""
    key = "2025-12-31 (FY)"
    fm: dict[tuple[str, str], StoredMetric] = {}
    for group in ("profitability", "cashflow", "efficiency", "leverage"):
        for result in compute_group(group, nee_10k, key):
            fm[(group, result.name)] = StoredMetric(result.value, dict(result.inputs))
    assert fm[NET].value == pytest.approx(5_332 / 27_412, rel=1e-3)
    assert [i for i in evaluate(fm) if i.rule_id == "DQ_REVENUE_POS"] == []
