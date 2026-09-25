"""User-tunable version constraints for ``quant`` (T-093).

T-090 gave ``quant`` exact version selection. T-093 adds, on top of it: ``>=`` / ``!=`` /
``latest`` and combinations in ``--metrics-version``; the same grammar for the return series,
the risk model and the corporate-action engine; named TOML profiles; ``quant versions``;
``--dry-run``; and the constraints recorded on the manifest. With no new flag every run must
behave, and record, exactly what it did before.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from portfolio_common.db import Database

from kg_schema.versions import (
    ALL_GROUPS,
    Clause,
    Constraint,
    VersionError,
    choose_constrained_versions,
    choose_versions,
    engine_version_key,
    parse_constraint,
    parse_metric_constraints,
    parse_metric_selection,
    pick_version,
    recognized_engine_versions,
    version_key,
)
from quant import cli
from quant.config import QuantSettings
from quant.manifest import resolve_quant_manifest
from quant.persist import plan_build_risk_model, plan_optimize, run_build_risk_model, run_optimize
from quant.profiles import load_profile
from quant.returns import run_build_returns
from quant.versions_report import versions_report

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
def db(memory_quant_db: Database, quant_seed: Callable[..., Database]) -> Database:
    """Six assets; return series under qret-v2 and qret-v3; market caps under metrics-v1,
    metrics-v2 and metrics-v3; a legacy corpact-v1-derived row next to the gateway corpact-v1."""
    conn = quant_seed(memory_quant_db, n_assets=6, n_days=280, with_dividends=True)
    for engine in ("qret-v2", "qret-v3"):
        run_build_returns(
            QuantSettings(db_path=Path(":memory:"), return_engine_version=engine),
            date_from="2000-01-01",
            date_to="2100-01-01",
            conn=conn,
        )
    conn.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) VALUES (1, 'DIVIDEND', '2024-03-01', 9.0, 'xbrl', "
        "'corpact-v1-derived', '2024-01-01T00:00:00Z')"
    )
    conn.execute(_METRICS_DDL)
    for asset in range(1, 7):
        filing_id = conn.execute(
            "INSERT INTO sec_filings (asset_id, form, fiscal_year, fiscal_period, period_end, "
            "retrieved_at) VALUES (?, '10-K', 2024, 'FY2024', '2024-12-31', "
            "'2025-01-01T00:00:00Z') RETURNING id",
            (asset,),
        ).fetchone()["id"]
        for version, cap in (
            ("metrics-v1", 1000.0),
            ("metrics-v2", 1000.0 * 4**asset),
            ("metrics-v3", 1000.0 * 2**asset),
        ):
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


def _counts(conn: Database) -> tuple[int, ...]:
    return tuple(
        int(conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])  # noqa: S608 - fixed names
        for t in ("quant_run", "quant_risk_model", "quant_portfolio", "quant_return_daily")
    )


# -- the grammar -------------------------------------------------------------------------------


def test_each_operator_parses_to_its_clause() -> None:
    assert parse_constraint("metrics-v2") == Constraint((Clause("=", "metrics-v2"),))
    assert parse_constraint("=metrics-v2") == Constraint((Clause("=", "metrics-v2"),))
    assert parse_constraint(">=metrics-v2") == Constraint((Clause(">=", "metrics-v2"),))
    assert parse_constraint("!=metrics-v1") == Constraint((Clause("!=", "metrics-v1"),))
    assert parse_constraint(">=metrics-v1,!=metrics-v3").clauses == (
        Clause(">=", "metrics-v1"),
        Clause("!=", "metrics-v3"),
    )
    for latest in (None, "", "  ", "latest"):
        assert parse_constraint(latest).is_latest


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (">=metrics-v2", "metrics-v3"),
        ("!=metrics-v3", "metrics-v2"),
        (">=metrics-v1,!=metrics-v3", "metrics-v2"),
        (">=pre-v1,!=metrics-v2,!=metrics-v3", "metrics-v1"),
        ("latest", "metrics-v3"),
        ("metrics-v1", "metrics-v1"),
    ],
)
def test_the_highest_satisfying_stored_version_wins(text: str, expected: str) -> None:
    stored = ["pre-v1", "metrics-v1", "metrics-v2", "metrics-v3"]
    assert pick_version(stored, parse_constraint(text), key=version_key, what="x") == expected


def test_an_unsatisfiable_constraint_names_why_each_candidate_was_rejected() -> None:
    stored = ["metrics-v1", "metrics-v2"]
    with pytest.raises(VersionError) as err:
        pick_version(
            stored, parse_constraint(">=metrics-v2,!=metrics-v2"), key=version_key, what="x"
        )
    msg = str(err.value)
    assert "metrics-v1 is older than metrics-v2" in msg
    assert "metrics-v2 is excluded by !=metrics-v2" in msg


@pytest.mark.parametrize(
    "bad", [">=", "metrics", ">=metrics-vX", "!=wat-v1", "a,,b", "<=metrics-v1"]
)
def test_a_malformed_constraint_fails_before_any_query(bad: str) -> None:
    with pytest.raises(VersionError):
        parse_constraint(bad)


# -- every T-090 form keeps its meaning ---------------------------------------------------------

_PRESENT = [
    {"valuation": ["metrics-v1", "metrics-v2"], "profitability": ["metrics-v2"]},
    {"valuation": ["metrics-v1"]},
    {"valuation": ["pre-v1", "metrics-v1"], "leverage": []},
    {},
]
_T090_FORMS = [
    None,
    "",
    "metrics-v1",
    "metrics-v2",
    "metrics-v9",
    "valuation=metrics-v1",
    "valuation=metrics-v2,profitability=metrics-v2",
    "valuation=metrics-v9",
    "profitability=metrics-v1",
    "nope=metrics-v1",
]


def _outcome(fn: Callable[[], object]) -> object:
    try:
        return fn()
    except VersionError as exc:
        return ("error", str(exc))


@pytest.mark.parametrize("present", _PRESENT)
@pytest.mark.parametrize("text", _T090_FORMS)
@pytest.mark.parametrize("groups", [("valuation",), ("valuation", "profitability", "leverage")])
def test_every_t090_form_resolves_exactly_as_before(
    present: dict[str, list[str]], text: str | None, groups: tuple[str, ...]
) -> None:
    old = _outcome(lambda: choose_versions(present, parse_metric_selection(text), groups=groups))
    new = _outcome(
        lambda: choose_constrained_versions(present, parse_metric_constraints(text), groups=groups)
    )
    assert new == old


def test_cycle_keeps_exact_selection_only() -> None:
    # cycle parses with T-090's parse_metric_selection, which has no operators: ">=" is not a group
    parsed = parse_metric_selection(">=metrics-v2")
    with pytest.raises(VersionError, match="unknown metric group"):
        choose_versions({"valuation": ["metrics-v2"]}, parsed)


def test_a_per_group_mix_of_constraints() -> None:
    selection = parse_metric_constraints("valuation>=metrics-v1,!=metrics-v3,profitability=latest")
    assert selection == {
        "valuation": Constraint((Clause(">=", "metrics-v1"), Clause("!=", "metrics-v3"))),
        "profitability": Constraint(),
    }
    present = {
        "valuation": ["metrics-v1", "metrics-v2", "metrics-v3"],
        "profitability": ["metrics-v1", "metrics-v4"],
        "leverage": ["metrics-v1", "metrics-v2"],
    }
    resolved = choose_constrained_versions(
        present, selection, groups=("valuation", "profitability", "leverage")
    )
    # leverage was not named: newest, as with T-090
    assert resolved.manifest() == {
        "leverage": "metrics-v2",
        "profitability": "metrics-v4",
        "valuation": "metrics-v2",
    }


def test_a_leading_groupless_constraint_applies_to_every_group() -> None:
    selection = parse_metric_constraints(">=metrics-v2")
    assert set(selection or {}) == {ALL_GROUPS}
    present = {"valuation": ["metrics-v1", "metrics-v2"], "leverage": ["metrics-v3"], "roic": []}
    resolved = choose_constrained_versions(
        present, selection, groups=("valuation", "leverage", "roic")
    )
    assert resolved.manifest() == {
        "leverage": "metrics-v3",
        "valuation": "metrics-v2",
    }  # roic empty
    with pytest.raises(VersionError, match="satisfying '>=metrics-v9'"):
        choose_constrained_versions({"valuation": []}, parse_metric_constraints(">=metrics-v9"))


def test_a_named_group_with_nothing_stored_is_an_error() -> None:
    with pytest.raises(VersionError, match="no metrics version for group 'valuation' is stored"):
        choose_constrained_versions(
            {}, parse_metric_constraints("valuation>=metrics-v1"), groups=("valuation",)
        )


# -- engine inputs ------------------------------------------------------------------------------


def test_engine_versions_order_by_number_and_reject_other_families() -> None:
    key = engine_version_key("qret")
    assert sorted(["qret-v10", "qret-v2", "qret-v1"], key=key) == ["qret-v1", "qret-v2", "qret-v10"]
    with pytest.raises(VersionError, match="not a qret version"):
        key("metrics-v2")
    good, other = recognized_engine_versions(
        ["corpact-v1", "corpact-v1-derived", "corpact-v0-approx", "corpact-v2"], "corpact"
    )
    assert good == ["corpact-v1", "corpact-v2"]
    assert other == ["corpact-v0-approx", "corpact-v1-derived"]


def test_the_return_series_is_constrained_against_what_is_stored(db: Database) -> None:
    assert resolve_quant_manifest(db, _settings()).return_engine_version == "qret-v2"  # configured
    latest = resolve_quant_manifest(db, _settings(returns_version="latest"))
    assert latest.return_engine_version == "qret-v3"
    assert resolve_quant_manifest(
        db, _settings(returns_version="!=qret-v3")
    ).return_engine_version == ("qret-v2")
    with pytest.raises(VersionError, match=r"qret-v2 is older than qret-v4.*qret-v3 is older"):
        resolve_quant_manifest(db, _settings(returns_version=">=qret-v4"))
    with pytest.raises(VersionError, match="not a qret version"):
        resolve_quant_manifest(db, _settings(returns_version="corpact-v1"))


def test_build_risk_model_reads_the_constrained_series(db: Database) -> None:
    res = run_build_risk_model(_settings(returns_version="latest"), as_of=_as_of(db), conn=db)
    row = db.execute(
        "SELECT panel_engine_version, manifest_json FROM quant_risk_model WHERE id = ?",
        (res.model_id,),
    ).fetchone()
    assert row["panel_engine_version"] == "qret-v3"
    manifest = json.loads(row["manifest_json"])
    assert manifest["returns"] == "qret-v3"
    assert manifest["constraints"] == {"returns": "latest"}


def test_corpact_pins_one_engine_and_ignores_history(db: Database) -> None:
    # A newer gateway engine for asset 1: the default per-asset priority would read it.
    db.execute(
        "INSERT INTO corporate_action (asset_id, action_type, ex_date, value, source, "
        "engine_version, ingested_at) VALUES (1, 'DIVIDEND', '2024-03-01', 7.5, "
        "'pricing-gateway', 'corpact-v2', '2024-01-01T00:00:00Z')"
    )

    def max_dividend(engine: str) -> float:
        return float(
            db.execute(
                "SELECT MAX(cash_dividend) FROM quant_return_daily "
                "WHERE engine_version = ? AND asset_id = 1",
                (engine,),
            ).fetchone()[0]
        )

    pinned = run_build_returns(
        _settings(corpact_version="corpact-v1", return_engine_version="qret-v7"),
        date_from="2000-01-01",
        date_to="2100-01-01",
        conn=db,
    )
    assert pinned.corpact_engine == "corpact-v1"
    assert max_dividend("qret-v7") < 7.5  # the pin held: corpact-v2 was not read

    latest = run_build_returns(
        _settings(corpact_version="latest", return_engine_version="qret-v8"),
        date_from="2000-01-01",
        date_to="2100-01-01",
        conn=db,
    )
    assert latest.corpact_engine == "corpact-v2"  # corpact-v1-derived is history, never read
    assert max_dividend("qret-v8") == pytest.approx(7.5)

    params = json.loads(
        db.execute(
            "SELECT params_json FROM quant_run WHERE command = 'build-returns' ORDER BY id DESC"
        ).fetchone()[0]
    )
    assert params["corpact_version"] == "latest"
    assert params["corpact_engine"] == "corpact-v2"
    with pytest.raises(VersionError, match="corpact-v2 is older than corpact-v3"):
        run_build_returns(
            _settings(corpact_version=">=corpact-v3"),
            date_from="2000-01-01",
            date_to="2100-01-01",
            conn=db,
        )


def test_the_risk_model_is_constrained_among_models_over_the_same_inputs(db: Database) -> None:
    as_of = _as_of(db)
    run_build_risk_model(_settings(), as_of=as_of, conn=db)
    run_build_risk_model(_settings(risk_model_version="rm-v2"), as_of=as_of, conn=db)

    default = run_optimize(_settings(), as_of=as_of, conn=db)
    newest = run_optimize(_settings(risk_model_select="latest"), as_of=as_of, conn=db)
    assert default.model_id != newest.model_id
    assert default.manifest_tag != newest.manifest_tag  # two risk models -> two sets of books
    kinds = db.execute("SELECT engine_version, manifest_json FROM quant_portfolio").fetchall()
    assert len(kinds) == 4  # nothing overwritten
    rm2 = [
        json.loads(k["manifest_json"])
        for k in kinds
        if k["engine_version"].endswith(newest.manifest_tag)
    ]
    assert all(
        m["risk_model"] == "rm-v2" and m["constraints"] == {"risk_model": "latest"} for m in rm2
    )

    with pytest.raises(VersionError, match="run build-risk-model first"):
        resolve_quant_manifest(db, _settings(risk_model_select=">=rm-v3"), optimize_as_of=as_of)


def test_the_default_risk_model_keeps_the_t090_book_key(db: Database) -> None:
    m = resolve_quant_manifest(db, _settings(), optimize_as_of=_as_of(db))
    assert m.book_tag == m.tag
    assert m.book_tagged("opt-v1") == m.tagged("opt-v1")


# -- the manifest records constraints but is keyed by what they resolve to ----------------------


def test_two_spellings_of_the_same_versions_share_a_tag(db: Database) -> None:
    a = resolve_quant_manifest(db, _settings(metrics_version="metrics-v2"))
    b = resolve_quant_manifest(db, _settings(metrics_version=">=metrics-v2,!=metrics-v3"))
    assert a.tag == b.tag
    assert json.loads(b.json())["constraints"] == {"metrics": ">=metrics-v2,!=metrics-v3"}
    assert "constraints" not in json.loads(resolve_quant_manifest(db, _settings()).json())


def test_two_runs_under_different_constraints_coexist(db: Database) -> None:
    as_of = _as_of(db)
    one = run_optimize(_settings(metrics_version="!=metrics-v3"), as_of=as_of, conn=db)
    two = run_optimize(
        _settings(metrics_version=">=metrics-v3", returns_version="latest"), as_of=as_of, conn=db
    )
    assert one.manifest_tag != two.manifest_tag
    assert one.model_id != two.model_id
    assert db.execute("SELECT COUNT(*) FROM quant_portfolio").fetchone()[0] == 4


def test_the_same_constraints_again_update_books_in_place(db: Database) -> None:
    as_of = _as_of(db)
    run_optimize(_settings(metrics_version="!=metrics-v3"), as_of=as_of, conn=db)
    run_optimize(_settings(metrics_version="!=metrics-v3"), as_of=as_of, conn=db)
    assert db.execute("SELECT COUNT(*) FROM quant_portfolio").fetchone()[0] == 2


# -- profiles -----------------------------------------------------------------------------------


def _profile(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "versions.toml"
    path.write_text(body)
    return path


def test_a_profile_is_parsed_and_validated(tmp_path: Path) -> None:
    path = _profile(
        tmp_path,
        '[profiles.baseline]\nmetrics = "metrics-v2"\n\n'
        '[profiles.pre_fix]\nmetrics = "valuation=metrics-v1"\nreturns = ">=qret-v1,!=qret-v3"\n'
        'risk_model = "latest"\ncorpact = "corpact-v1"\n',
    )
    pre = load_profile(path, "pre_fix")
    assert pre.settings_updates() == {
        "metrics_version": "valuation=metrics-v1",
        "returns_version": ">=qret-v1,!=qret-v3",
        "risk_model_select": "latest",
        "corpact_version": "corpact-v1",
    }
    assert pre.label == f"pre_fix ({path})"
    with pytest.raises(VersionError, match="pick one with --profile"):
        load_profile(path)
    with pytest.raises(VersionError, match="it defines: baseline, pre_fix"):
        load_profile(path, "nope")
    only = _profile(tmp_path, '[profiles.solo]\nreturns = "qret-v2"\n')
    assert load_profile(only).name == "solo"  # one profile needs no --profile


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("not toml [", "not valid TOML"),
        ("[other]\nx = 1\n", "no \\[profiles"),
        ('[profiles.p]\nmetric = "metrics-v1"\n', "unknown key"),
        ("[profiles.p]\nreturns = 2\n", "must be a string"),
        ('[profiles.p]\nreturns = "metrics-v1"\n', "not a qret version"),
    ],
)
def test_a_bad_profile_is_rejected(tmp_path: Path, body: str, match: str) -> None:
    with pytest.raises(VersionError, match=match):
        load_profile(_profile(tmp_path, body))


def test_a_flag_wins_over_the_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KG_FINANCIAL_DB", str(tmp_path / "x.db"))
    path = _profile(tmp_path, '[profiles.p]\nmetrics = "metrics-v1"\nreturns = "qret-v2"\n')
    args = cli.build_parser().parse_args(
        ["optimize", "--version-profile", str(path), "--metrics-version", ">=metrics-v2"]
    )
    settings = cli._settings(args)
    assert settings.metrics_version == ">=metrics-v2"  # the flag
    assert settings.returns_version == "qret-v2"  # the profile
    assert settings.version_profile == f"p ({path})"


# -- the CLI: versions, dry run, flags ----------------------------------------------------------


class _NoClose:
    def __init__(self, conn: Database) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> object:
        return getattr(self._conn, name)

    def close(self) -> None:
        pass


@pytest.fixture
def cli_on(db: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Database]:
    monkeypatch.setenv("KG_FINANCIAL_DB", str(tmp_path / "unused.db"))
    for module in ("quant.cli", "quant.persist", "quant.returns"):
        monkeypatch.setattr(f"{module}.connect", lambda *_a, **_k: _NoClose(db))
    yield db


def test_versions_lists_every_input(cli_on: Database, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["versions"]) == 0
    out = capsys.readouterr().out
    assert "metrics (--metrics-version; quant reads: valuation)" in out
    assert "metrics-v3" in out and "newest (default)" in out
    assert "qret-v2" in out and "configured" in out and "qret-v3" in out
    assert "corpact-v1-derived" in out and "not a version quant reads (history)" in out
    assert "risk model" in out and "nothing stored" in out


def test_versions_report_counts_rows(db: Database) -> None:
    text = versions_report(db, _settings())
    n_qret = db.execute(
        "SELECT COUNT(*) FROM quant_return_daily WHERE engine_version = 'qret-v2'"
    ).fetchone()[0]
    assert f"{n_qret:,} rows" in text


def test_a_dry_run_writes_nothing(cli_on: Database, capsys: pytest.CaptureFixture[str]) -> None:
    as_of = _as_of(cli_on)
    before = _counts(cli_on)
    assert (
        cli.main(
            [
                "build-risk-model",
                "--analysis-date",
                as_of,
                "--dry-run",
                "--returns-version",
                "latest",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            ["optimize", "--analysis-date", as_of, "--dry-run", "--metrics-version", "!=metrics-v3"]
        )
        == 0
    )
    assert _counts(cli_on) == before
    out = capsys.readouterr().out
    assert "nothing written" in out
    assert '"returns":"qret-v3"' in out
    assert "not stored, would be built" in out
    assert "books: engine_version opt-v1+" in out


def test_plans_match_what_the_real_runs_then_write(db: Database) -> None:
    as_of = _as_of(db)
    settings = _settings(metrics_version="metrics-v1")
    plan = plan_optimize(settings, as_of=as_of, conn=db)
    res = run_optimize(settings, as_of=as_of, conn=db)
    stored = db.execute(
        "SELECT model_version FROM quant_risk_model WHERE id = ?", (res.model_id,)
    ).fetchone()[0]
    assert stored == plan.model_version
    assert plan.book_version == f"opt-v1+{res.manifest_tag}"
    assert plan_build_risk_model(settings, as_of=as_of, conn=db).model_stored


def test_an_unsatisfiable_flag_exits_1_with_the_reasons(
    cli_on: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    as_of = _as_of(cli_on)
    assert cli.main(["optimize", "--analysis-date", as_of, "--returns-version", ">=qret-v9"]) == 1
    assert "qret-v3 is older than qret-v9" in capsys.readouterr().err
    assert cli.main(["build-returns", "--corpact-version", "corpact-v5"]) == 1
    assert "corpact-v1 is not corpact-v5" in capsys.readouterr().err


def test_a_bad_profile_exits_1(
    cli_on: Database, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["optimize", "--version-profile", str(tmp_path / "missing.toml")]) == 1
    assert "cannot read version profile" in capsys.readouterr().err
    assert cli.main(["optimize", "--profile", "x"]) == 1
    assert "--profile needs --version-profile" in capsys.readouterr().err


def test_the_two_risk_model_flags_are_exclusive(cli_on: Database) -> None:
    with pytest.raises(SystemExit):
        cli.main(["optimize", "--model-version", "rm-v2", "--risk-model-version", "latest"])


def test_the_new_flags_reach_the_settings() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["optimize", "--returns-version", "latest", "--risk-model-version", ">=rm-v1"]
    )
    assert (args.returns_version, args.risk_model_select) == ("latest", ">=rm-v1")
    assert (
        parser.parse_args(["build-risk-model", "--returns-version", "qret-v2"]).returns_version
        == "qret-v2"
    )
    assert (
        parser.parse_args(["build-returns", "--corpact-version", "latest"]).corpact_version
        == "latest"
    )
    for flag, field in (
        ("returns_version", "returns_version"),
        ("risk_model_select", "risk_model_select"),
        ("corpact_version", "corpact_version"),
    ):
        assert cli._FLAG_TO_FIELD[flag][0] == field
