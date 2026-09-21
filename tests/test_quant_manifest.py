"""Runs over different input versions coexist instead of overwriting each other (T-090).

``quant_risk_model`` and ``quant_portfolio`` used to be keyed by constant version strings and
written with ``ON CONFLICT ... DO UPDATE``, so re-running over a newer ``fundamental_metrics``
engine silently replaced the earlier result. The run's *manifest* tag is now folded into those
keys, so different inputs give parallel rows and the same inputs update in place.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from portfolio_common.db import Database

from kg_schema.versions import VersionError
from quant.cli import _FLAG_TO_FIELD, _run_build_risk_model, _run_optimize, build_parser
from quant.config import QuantSettings
from quant.evaluate import run_evaluate
from quant.manifest import resolve_quant_manifest
from quant.persist import (
    OptimizeRunResult,
    RiskModelResult,
    run_build_risk_model,
    run_optimize,
)
from quant.returns import run_build_returns

# The migrated shape: engine_version is part of the key, so versions can coexist (as in production).
_METRICS_DDL = """
CREATE TABLE fundamental_metrics (
    id INTEGER PRIMARY KEY,
    filing_id INTEGER NOT NULL REFERENCES sec_filings(id) ON DELETE CASCADE,
    metric_group TEXT NOT NULL, metric_name TEXT NOT NULL, value REAL, unit TEXT,
    inputs_json TEXT, computed_at TEXT NOT NULL, engine_version TEXT NOT NULL,
    event_time TEXT, run_id INTEGER,
    UNIQUE (filing_id, metric_group, metric_name, engine_version)
)
"""


def _settings(**over: object) -> QuantSettings:
    base: dict[str, object] = {
        "db_path": Path(":memory:"),
        "lookback_days": 200,
        "min_history_days": 140,
        "liquidity_min_dollar_volume": 0.0,
        "max_name_weight": None,
        "max_sector_weight": None,
        "objectives": ["min_var", "tangency"],
        "frontier_k": 4,
    }
    base.update(over)
    return QuantSettings(**base)  # type: ignore[arg-type]


@pytest.fixture
def two_versions(memory_quant_db: Database, quant_seed: Callable[..., Database]) -> Database:
    """Six assets with prices, and market caps stored under metrics-v1 (all equal) and
    metrics-v2 (very unequal) -- so the equilibrium expected returns genuinely differ."""
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=280, with_dividends=True)
    run_build_returns(
        QuantSettings(db_path=Path(":memory:")),
        date_from="2000-01-01",
        date_to="2100-01-01",
        conn=conn,
    )
    conn.execute(_METRICS_DDL)
    for asset in range(1, 7):
        filing_id = conn.execute(
            "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
            "retrieved_at) VALUES (?, '10-K', 2024, 'FY2024', '2024-12-31', "
            "'2025-01-01T00:00:00Z') RETURNING id",
            (asset,),
        ).fetchone()["id"]
        for version, cap in (("metrics-v1", 1000.0), ("metrics-v2", 1000.0 * 4**asset)):
            conn.execute(
                "INSERT INTO fundamental_metrics (filing_id, metric_group, metric_name, value, "
                "unit, inputs_json, computed_at, engine_version) VALUES "
                "(?, 'valuation', 'market_capitalization', ?, 'usd', ?, '2025-01-01T00:00:00Z', ?)",
                (filing_id, cap, json.dumps({"market_capitalization": cap}), version),
            )
    conn.commit()
    return conn


def _as_of(conn: Database) -> str:
    return str(conn.execute("SELECT MAX(obs_date) FROM quant_return_daily").fetchone()[0])


def _models(conn: Database) -> list[tuple[int, str, dict[str, object]]]:
    return [
        (int(r["id"]), str(r["model_version"]), json.loads(r["manifest_json"]))
        for r in conn.execute(
            "SELECT id, model_version, manifest_json FROM quant_risk_model ORDER BY id"
        )
    ]


def _equilibrium(conn: Database, model_id: int) -> dict[int, float]:
    return {
        int(r["asset_id"]): float(r["mu"])
        for r in conn.execute(
            "SELECT asset_id, mu FROM quant_expected_return WHERE model_id = ? "
            "AND mu_model = 'equilibrium'",
            (model_id,),
        )
    }


# -- the risk model --------------------------------------------------------------------------


def test_two_manifests_write_two_risk_models_that_coexist(two_versions: Database) -> None:
    conn = two_versions
    as_of = _as_of(conn)
    newest = run_build_risk_model(_settings(), as_of=as_of, conn=conn)  # default -> metrics-v2
    older = run_build_risk_model(_settings(metrics_version="metrics-v1"), as_of=as_of, conn=conn)

    models = _models(conn)
    assert len(models) == 2  # the second did NOT overwrite the first
    assert newest.manifest_tag != older.manifest_tag
    versions = {m[1] for m in models}
    assert versions == {f"rm-v1+{newest.manifest_tag}", f"rm-v1+{older.manifest_tag}"}
    by_id = {m[0]: m[2] for m in models}
    assert by_id[newest.model_id]["metrics"] == {"valuation": "metrics-v2"}
    assert by_id[older.model_id]["metrics"] == {"valuation": "metrics-v1"}
    assert by_id[newest.model_id]["returns"] == "qret-v2"

    # the inputs genuinely differed, and the two results kept their own numbers
    assert _equilibrium(conn, newest.model_id) != _equilibrium(conn, older.model_id)


def test_the_same_manifest_updates_in_place(two_versions: Database) -> None:
    conn = two_versions
    as_of = _as_of(conn)
    first = run_build_risk_model(_settings(), as_of=as_of, conn=conn)
    again = run_build_risk_model(_settings(), as_of=as_of, conn=conn)
    assert again.manifest_tag == first.manifest_tag  # deterministic
    assert again.model_id == first.model_id
    assert len(_models(conn)) == 1  # idempotent: a re-run refreshes, it does not fork


def test_the_run_records_its_manifest_and_the_view_exposes_it(two_versions: Database) -> None:
    conn = two_versions
    res = run_build_risk_model(_settings(), as_of=_as_of(conn), conn=conn)
    params = json.loads(
        conn.execute(
            "SELECT params_json FROM quant_run WHERE command = 'build-risk-model'"
        ).fetchone()[0]
    )
    assert params["manifest_tag"] == res.manifest_tag
    assert params["manifest"]["metrics"] == {"valuation": "metrics-v2"}
    view = conn.execute("SELECT manifest_json FROM v_quant_risk_model").fetchone()[0]
    assert json.loads(view)["consumer"] == "quant"


# -- the books --------------------------------------------------------------------------------


def test_optimize_writes_parallel_books_each_tied_to_its_own_model(two_versions: Database) -> None:
    conn = two_versions
    as_of = _as_of(conn)
    new = run_optimize(_settings(), as_of=as_of, conn=conn)
    old = run_optimize(_settings(metrics_version="metrics-v1"), as_of=as_of, conn=conn)

    assert new.manifest_tag != old.manifest_tag
    books = conn.execute(
        "SELECT p.id, p.kind, p.engine_version, p.model_id, p.manifest_json AS book_manifest, "
        "m.manifest_json AS model_manifest FROM quant_portfolio p "
        "JOIN quant_risk_model m ON m.id = p.model_id ORDER BY p.id"
    ).fetchall()
    assert len(books) == 4  # min_var + tangency, once per manifest -- none overwritten
    assert {b["engine_version"] for b in books} == {
        f"opt-v1+{new.manifest_tag}",
        f"opt-v1+{old.manifest_tag}",
    }
    for b in books:  # every book is built on the model with the *same* manifest
        assert b["book_manifest"] == b["model_manifest"]
    assert {b["model_id"] for b in books} == {new.model_id, old.model_id}
    for pid in [*new.books.values(), *old.books.values()]:
        n = conn.execute(
            "SELECT COUNT(*) FROM quant_position WHERE portfolio_id = ?", (pid,)
        ).fetchone()[0]
        assert n > 0  # each book has its own positions


def test_optimize_reuses_the_model_of_its_own_manifest(two_versions: Database) -> None:
    conn = two_versions
    as_of = _as_of(conn)
    built = run_build_risk_model(_settings(metrics_version="metrics-v1"), as_of=as_of, conn=conn)
    opt = run_optimize(_settings(metrics_version="metrics-v1"), as_of=as_of, conn=conn)
    assert opt.model_id == built.model_id  # found by its tagged version, not rebuilt
    assert len(_models(conn)) == 1


def test_evaluate_compares_the_books_of_every_manifest(two_versions: Database) -> None:
    conn = two_versions
    dates = [
        r[0]
        for r in conn.execute("SELECT DISTINCT obs_date FROM quant_return_daily ORDER BY obs_date")
    ]
    as_of, end = dates[-25], dates[-1]
    run_optimize(_settings(objectives=["min_var"]), as_of=as_of, conn=conn)
    run_optimize(
        _settings(objectives=["min_var"], metrics_version="metrics-v1"), as_of=as_of, conn=conn
    )

    result = run_evaluate(_settings(), date_to=end, conn=conn)

    assert result.perf_rows > 0
    evaluated = conn.execute(
        "SELECT DISTINCT b.portfolio_id FROM quant_benchmark_performance b "
        "JOIN quant_portfolio p ON p.id = b.portfolio_id WHERE p.kind = 'min_var'"
    ).fetchall()
    assert len(evaluated) == 2  # both manifests' books were evaluated side by side


# -- failure modes ----------------------------------------------------------------------------


def test_an_unstored_version_fails_before_any_run_is_written(two_versions: Database) -> None:
    conn = two_versions
    before = conn.execute("SELECT COUNT(*) FROM quant_run").fetchone()[0]
    with pytest.raises(VersionError, match="metrics-v9"):
        run_build_risk_model(_settings(metrics_version="metrics-v9"), as_of=_as_of(conn), conn=conn)
    with pytest.raises(VersionError, match="metrics-v9"):
        run_optimize(_settings(metrics_version="metrics-v9"), as_of=_as_of(conn), conn=conn)
    assert conn.execute("SELECT COUNT(*) FROM quant_run").fetchone()[0] == before
    assert _models(conn) == []


def test_a_selection_naming_a_group_quant_does_not_read_is_rejected(two_versions: Database) -> None:
    with pytest.raises(VersionError, match="not read by this consumer"):
        resolve_quant_manifest(two_versions, _settings(metrics_version="profitability=metrics-v1"))


def test_the_return_engine_is_part_of_the_manifest(two_versions: Database) -> None:
    a = resolve_quant_manifest(two_versions, _settings())
    b = resolve_quant_manifest(two_versions, _settings(return_engine_version="qret-v3"))
    assert a.tag != b.tag  # a different return series must not overwrite the first model
    assert a.tagged("rm-v1") == f"rm-v1+{a.tag}"


# -- the CLI ---------------------------------------------------------------------------------


def test_the_metrics_version_flag_exists_and_reaches_the_settings() -> None:
    parser = build_parser()
    for command in ("build-risk-model", "optimize"):
        args = parser.parse_args([command, "--metrics-version", "valuation=metrics-v1"])
        assert args.metrics_version == "valuation=metrics-v1"
        assert parser.parse_args([command]).metrics_version is None
    assert _FLAG_TO_FIELD["metrics_version"][0] == "metrics_version"


def test_the_cli_reports_the_manifest_and_exits_1_on_an_unstored_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def ok_model(*_a: object, **_k: object) -> RiskModelResult:
        return RiskModelResult(7, "2026-01-02", 6, "ledoit_wolf_cc", 0.1, 10, True, "abc12345")

    monkeypatch.setattr("quant.cli.run_build_risk_model", ok_model)
    assert _run_build_risk_model(_settings(), "2026-01-02", store_cov=True) == 0
    assert "manifest abc12345" in capsys.readouterr().out

    def ok_opt(*_a: object, **_k: object) -> OptimizeRunResult:
        return OptimizeRunResult(7, "2026-01-02", {"min_var": 1}, 0, "abc12345")

    monkeypatch.setattr("quant.cli.run_optimize", ok_opt)
    assert _run_optimize(_settings(), "2026-01-02") == 0
    assert "manifest abc12345" in capsys.readouterr().out

    def unstored(*_a: object, **_k: object) -> None:
        raise VersionError("metrics version 'metrics-v9' is not stored")

    monkeypatch.setattr("quant.cli.run_build_risk_model", unstored)
    monkeypatch.setattr("quant.cli.run_optimize", unstored)
    assert _run_build_risk_model(_settings(), "2026-01-02", store_cov=True) == 1
    assert _run_optimize(_settings(), "2026-01-02") == 1
    assert "metrics-v9" in capsys.readouterr().err
