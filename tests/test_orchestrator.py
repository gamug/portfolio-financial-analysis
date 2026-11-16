"""The orchestrator (T-011): the step table, argv, the runner, run-id capture, the
news skip, failure stop, and the CLI wrapper. Hermetic: ``run_command`` is always
replaced, no agent is ever spawned."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

import api.__main__ as entry
import api.contract
import kg_schema
from api import orchestrator as orch
from api.config import DEFAULT_NEWS_DB
from api.routers.run import CapsRequest, ConstructionRequest, DateOnlyRequest
from cycle.construction import SCHEMES
from entity_resolution.config import DEFAULT_NEWS_DB as ER_DEFAULT

_QUANT_RUN = """
INSERT INTO quant_run (command, started_at, status, engine_version)
VALUES (?, '2026-10-02T00:00:00Z', 'completed', 'quant-v1')
"""


def _paths(tmp_path: Path, *, news: Path | None = None) -> orch.RunPaths:
    return orch.RunPaths(
        db_path=tmp_path / "fin.db",
        universe_db_path=tmp_path / "universe.db",
        news_db_path=news if news is not None else tmp_path / "missing_news.db",
    )


def _ctx(  # noqa: PLR0913 - every construction setting, all optional
    tmp_path: Path,
    *,
    news: Path | None = None,
    top_n: int | None = None,
    weight_scheme: str | None = None,
    max_name_weight: float | None = None,
    max_sector_weight: float | None = None,
) -> orch.RunContext:
    return orch.RunContext(
        paths=_paths(tmp_path, news=news),
        analysis_date="2026-10-02",
        top_n=top_n,
        weight_scheme=weight_scheme,
        max_name_weight=max_name_weight,
        max_sector_weight=max_sector_weight,
    )


def _touch(path: Path) -> Path:
    path.write_text("")
    return path


class FakeRunner:
    """Records every (argv, env) and answers by ``(package, command)``; an entry in
    ``errors`` raises instead (to exercise a step that never returns).

    A ``pending`` check (T-017) is answered from ``checks`` by package -- default ``pending: yes
    test`` -- and recorded in ``check_calls``, never in ``calls``, so a test about the steps
    reads the steps only."""

    def __init__(
        self,
        answers: dict[tuple[str, str], tuple[int, str]] | None = None,
        errors: dict[tuple[str, str], BaseException] | None = None,
        checks: Mapping[str, tuple[int, str] | BaseException] | None = None,
    ) -> None:
        self.answers = answers or {}
        self.errors = errors or {}
        self.checks: Mapping[str, tuple[int, str] | BaseException] = checks or {}
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.check_calls: list[tuple[list[str], dict[str, str], float | None]] = []
        self.on_call: Callable[[list[str]], None] | None = None

    def __call__(
        self, argv: list[str], env: dict[str, str], timeout: float | None = None
    ) -> orch.CommandResult:
        if argv[3] == "pending":
            self.check_calls.append((list(argv), dict(env), timeout))
            answer = self.checks.get(argv[2], (0, "pending: yes test"))
            if isinstance(answer, BaseException):
                raise answer
            return orch.CommandResult(returncode=answer[0], output=answer[1])
        assert timeout is None  # a step has no timeout
        self.calls.append((list(argv), dict(env)))
        if self.on_call is not None:
            self.on_call(argv)
        key = (argv[2], argv[3])
        if key in self.errors:
            raise self.errors[key]
        code, output = self.answers.get(key, (0, ""))
        return orch.CommandResult(returncode=code, output=output)

    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


def _write_quant_run(path: Path, command: str) -> None:
    conn = kg_schema.connect(path)
    conn.create_schema(api.contract.QUANT_RUN_DDL)
    conn.execute(_QUANT_RUN, (command,))
    conn.commit()
    conn.close()


# -- argv ---------------------------------------------------------------------------------------


def test_pipeline_argv_without_settings(tmp_path: Path) -> None:
    run = orch.new_run("pipeline", _ctx(tmp_path))
    assert [s.argv for s in run.steps] == [
        [
            "-m",
            "pricing_agent",
            "run",
            "--store-daily",
            "--observations",
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
        ],
        [
            "-m",
            "fundamental_agent",
            "run",
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
        ],
        [
            "-m",
            "entity_resolution",
            "build",
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
            "--news-db",
            str(tmp_path / "missing_news.db"),
        ],
        [
            "-m",
            "cycle",
            "select",
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
        ],
        [
            "-m",
            "quant",
            "backfill-actions",
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
        ],
        [
            "-m",
            "quant",
            "build-returns",
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
        ],
        [
            "-m",
            "quant",
            "build-risk-model",
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
        ],
        [
            "-m",
            "quant",
            "optimize",
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
        ],
        [
            "-m",
            "quant",
            "evaluate",
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
        ],
    ]


def test_pipeline_forwards_settings_only_to_the_steps_that_accept_them(tmp_path: Path) -> None:
    ctx = _ctx(
        tmp_path, top_n=30, weight_scheme="equal", max_name_weight=0.1, max_sector_weight=0.3
    )
    argv = {s.name: s.argv for s in orch.new_run("pipeline", ctx).steps}
    # D3: top_n + caps to cycle select, build-risk-model and optimize; scheme to select only.
    assert argv["cycle_select"][3:11] == [
        "--top-n",
        "30",
        "--weight-scheme",
        "equal",
        "--max-name-weight",
        "0.1",
        "--max-sector-weight",
        "0.3",
    ]
    for name in ("quant_risk_model", "quant_optimize"):
        assert argv[name][3:9] == [
            "--top-n",
            "30",
            "--max-name-weight",
            "0.1",
            "--max-sector-weight",
            "0.3",
        ]
        assert "--weight-scheme" not in argv[name]
    for name in (
        "pricing",
        "fundamental",
        "entity_resolution",
        "quant_actions",
        "quant_returns",
        "quant_evaluate",
    ):
        assert not any(
            a.startswith("--top-n") or "weight" in a or "--weight-scheme" in a for a in argv[name]
        ), name


def test_single_step_endpoint_argv(tmp_path: Path) -> None:
    expected = {
        "pricing-agent": ["-m", "pricing_agent", "run", "--store-daily", "--observations"],
        "fundamental-agent": ["-m", "fundamental_agent", "run"],
        "entity-resolution": ["-m", "entity_resolution", "build"],
        "cycle-select": ["-m", "cycle", "select"],
        "cycle-monitor": ["-m", "cycle", "monitor"],
        "quant-backfill-actions": ["-m", "quant", "backfill-actions"],
        "quant-build-returns": ["-m", "quant", "build-returns"],
        "quant-build-risk-model": ["-m", "quant", "build-risk-model"],
        "quant-optimize": ["-m", "quant", "optimize"],
        "quant-evaluate": ["-m", "quant", "evaluate"],
    }
    for kind, head in expected.items():
        steps = orch.new_run(kind, _ctx(tmp_path)).steps
        assert len(steps) == 1, kind
        argv = steps[0].argv
        assert argv[: len(head)] == head, kind
        assert argv[argv.index("--analysis-date") + 1] == "2026-10-02"
        assert "--db" in argv and "--universe-db" in argv
        if kind == "entity-resolution":
            assert "--news-db" in argv


# -- forbidding the forbidden -------------------------------------------------------------------


_FORBIDDEN = (
    "--pin",
    "--exclude",
    "--only-sectors",
    "--allow",
    "--force",
    "--fresh",
    "migrate",
)


def test_no_setting_combination_produces_a_forbidden_flag(tmp_path: Path) -> None:
    contexts = [
        _ctx(tmp_path),
        _ctx(tmp_path, top_n=1),
        _ctx(tmp_path, weight_scheme="score_tilt"),
        _ctx(tmp_path, max_name_weight=0.5),
        _ctx(tmp_path, max_sector_weight=0.5),
        _ctx(tmp_path, top_n=30, weight_scheme="equal", max_name_weight=0.1, max_sector_weight=0.3),
    ]
    kinds = ["pipeline", *orch._SINGLE_STEPS]
    for kind in kinds:
        for ctx in contexts:
            for step in orch.new_run(kind, ctx).steps:
                blob = " ".join(step.argv)
                for token in _FORBIDDEN:
                    assert token not in blob, f"{kind} {step.name}: {token}"


def test_the_cli_help_names_no_forbidden_flag() -> None:
    help_text = orch.build_cli_parser().format_help()
    for token in _FORBIDDEN:
        assert token not in help_text, token


def test_request_models_forbid_extra_and_omni_fields() -> None:
    forbidden = {
        "pin",
        "exclude",
        "only_sector",
        "allow",
        "force",
        "fresh",
        "migrate",
        "db",
        "universe",
        "ticker",
    }
    for model in (DateOnlyRequest, CapsRequest, ConstructionRequest):
        assert model.model_config["extra"] == "forbid"
        assert set(model.model_fields) == (
            {"analysis_date"}
            | (
                {"top_n", "max_name_weight", "max_sector_weight"}
                if model is not DateOnlyRequest
                else set()
            )
            | ({"weight_scheme"} if model is ConstructionRequest else set())
        )
        assert not any(part in name for name in model.model_fields for part in forbidden)


# -- env and run-id capture ---------------------------------------------------------------------


def test_child_env_carries_all_three_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner()
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("pipeline", _ctx(tmp_path, news=news))
    assert run.status == "completed"
    assert len(fake.calls) == 9
    for _, env in fake.calls:
        assert env["KG_FINANCIAL_DB"] == str(tmp_path / "fin.db")
        assert env["KG_UNIVERSE_DB"] == str(tmp_path / "universe.db")
        assert env["KG_NEWS_DB"] == str(news)


def test_run_id_capture_reads_the_printed_line_and_the_quant_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    db = tmp_path / "fin.db"
    answers = {
        ("pricing_agent", "run"): (0, "\nrun 11: 0 priced (of 0 tickers)"),
        ("fundamental_agent", "run"): (0, "\nrun 22: 0 analyzed (of 0 planned)"),
        ("entity_resolution", "build"): (0, "cycle 33: 0 shared-executive edges"),
        ("cycle", "select"): (0, "select 44 2026-10-02: 0 selected"),
    }
    fake = FakeRunner(answers)

    def insert(argv: list[str]) -> None:
        if argv[2] == "quant":
            _write_quant_run(db, argv[3])

    fake.on_call = insert
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("pipeline", _ctx(tmp_path, news=news))
    ids = {s.name: s.run_id for s in run.steps}
    assert ids == {
        "pricing": 11,
        "fundamental": 22,
        "entity_resolution": 33,
        "cycle_select": 44,
        "quant_actions": 1,
        "quant_returns": 2,
        "quant_risk_model": 3,
        "quant_optimize": 4,
        "quant_evaluate": 5,
    }
    assert not any("run_id not captured" in (s.reason or "") for s in run.steps)


def test_a_completed_step_without_a_run_line_gets_the_capture_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner({("pricing_agent", "run"): (0, "priced everything, no id printed")})
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("pricing-agent", _ctx(tmp_path))
    step = run.steps[0]
    assert step.status == "completed"
    assert step.run_id is None
    assert step.reason == "run_id not captured"


def test_quant_run_id_ignores_a_stale_row_and_before_is_zero_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "fin.db"
    _write_quant_run(db, "optimize")  # a stale row (id 1) from an earlier run
    fake = FakeRunner()

    def _insert(argv: list[str]) -> None:
        if argv[2] == "quant":
            _write_quant_run(Path(argv[argv.index("--db") + 1]), argv[3])

    fake.on_call = _insert
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("quant-optimize", _ctx(tmp_path))
    assert run.steps[0].run_id == 2  # the row this step created, not the stale one

    # A fresh database: the file and the table do not exist before the step -> before is 0.
    fresh = tmp_path / "fresh.db"
    assert not fresh.exists()
    run2 = orch.run_now(
        "quant-optimize",
        orch.RunContext(
            paths=orch.RunPaths(fresh, tmp_path / "universe.db", tmp_path / "missing_news.db"),
            analysis_date="2026-10-02",
        ),
    )
    assert run2.steps[0].run_id == 1


# -- failure isolation (T-013) and the news skip ------------------------------------------------


_OK_ANSWERS = {
    ("pricing_agent", "run"): (0, "\nrun 1: ok"),
    ("fundamental_agent", "run"): (0, "\nrun 2: ok"),
    ("entity_resolution", "build"): (0, "cycle 3: 0 edges"),
    ("cycle", "select"): (0, "select 4 2026-10-02: ok"),
}


def _statuses(run: orch.RunState) -> dict[str, str]:
    return {s.name: s.status for s in run.steps}


def _reasons(run: orch.RunState) -> dict[str, str | None]:
    return {s.name: s.reason for s in run.steps if s.status == "blocked"}


def test_dependency_table_pins_the_edges() -> None:
    table = {s.name: s.depends_on for s in orch._PIPELINE}
    assert table == {
        "pricing": (),
        "fundamental": (),
        "entity_resolution": (),
        "cycle_select": ("pricing", "fundamental"),
        "quant_actions": ("pricing", "fundamental"),
        "quant_returns": ("quant_actions",),
        "quant_risk_model": ("quant_returns",),
        "quant_optimize": ("quant_risk_model",),
        "quant_evaluate": ("quant_optimize", "cycle_select"),
    }
    names = [s.name for s in orch._PIPELINE]
    for step, upstreams in table.items():
        for upstream in upstreams:
            assert upstream in names  # every dependency names a real step
            assert names.index(upstream) < names.index(step)  # and precedes its dependent
    assert orch._MONITOR.depends_on == ()


def test_entity_resolution_failure_isolates_the_other_eight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner(
        {**_OK_ANSWERS, ("entity_resolution", "build"): (1, "urls.db is not a database")}
    )
    monkeypatch.setattr(orch, "run_command", fake)
    code = orch.cli_main(
        [
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
            "--news-db",
            str(news),
        ]
    )
    assert code == 1  # the run failed, so the CLI exits 1
    assert len(fake.calls) == 9  # every step ran: nothing depends on entity_resolution
    rows = {
        r["step"]: r["status"]
        for r in _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_step")
    }
    assert rows["entity_resolution"] == "failed"
    assert all(v == "completed" for k, v in rows.items() if k != "entity_resolution")


def test_pricing_failure_blocks_cycle_and_quant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner({**_OK_ANSWERS, ("pricing_agent", "run"): (1, "gateway refused")})
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("pipeline", _ctx(tmp_path, news=news))
    assert run.status == "failed"
    assert _statuses(run) == {
        "pricing": "failed",
        "fundamental": "completed",
        "entity_resolution": "completed",
        "cycle_select": "blocked",
        "quant_actions": "blocked",
        "quant_returns": "blocked",
        "quant_risk_model": "blocked",
        "quant_optimize": "blocked",
        "quant_evaluate": "blocked",
    }
    assert _reasons(run) == {
        "cycle_select": "blocked by pricing (failed)",
        "quant_actions": "blocked by pricing (failed)",
        "quant_returns": "blocked by quant_actions (blocked)",
        "quant_risk_model": "blocked by quant_returns (blocked)",
        "quant_optimize": "blocked by quant_risk_model (blocked)",
        "quant_evaluate": "blocked by cycle_select (blocked), quant_optimize (blocked)",
    }
    assert len(fake.calls) == 3  # no child was started for a blocked step
    # the blocked row is persisted with NULL timings/exit/run_id
    blocked = {
        r["step"]: r
        for r in _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_step ORDER BY ordinal")
    }["cycle_select"]
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "blocked by pricing (failed)"
    assert blocked["started_at"] is None and blocked["finished_at"] is None
    assert blocked["exit_code"] is None and blocked["run_id"] is None


def test_fundamental_failure_blocks_cycle_and_quant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner({**_OK_ANSWERS, ("fundamental_agent", "run"): (1, "no filings, refusing")})
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("pipeline", _ctx(tmp_path, news=news))
    statuses = _statuses(run)
    assert run.status == "failed"
    assert statuses["fundamental"] == "failed"
    assert statuses["pricing"] == "completed"
    assert statuses["entity_resolution"] == "completed"
    assert all(
        statuses[n] == "blocked"
        for n in (
            "cycle_select",
            "quant_actions",
            "quant_returns",
            "quant_risk_model",
            "quant_optimize",
            "quant_evaluate",
        )
    )
    reasons = _reasons(run)
    assert reasons["cycle_select"] == "blocked by fundamental (failed)"
    assert reasons["quant_actions"] == "blocked by fundamental (failed)"
    assert (
        reasons["quant_evaluate"] == "blocked by cycle_select (blocked), quant_optimize (blocked)"
    )
    assert len(fake.calls) == 3


def test_cycle_select_failure_blocks_only_quant_evaluate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner({**_OK_ANSWERS, ("cycle", "select"): (1, "dq-v2 vs dq-v3")})
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("pipeline", _ctx(tmp_path, news=news))
    statuses = _statuses(run)
    assert run.status == "failed"
    assert statuses["cycle_select"] == "failed"
    assert all(
        statuses[n] == "completed"
        for n in ("quant_actions", "quant_returns", "quant_risk_model", "quant_optimize")
    )
    assert statuses["quant_evaluate"] == "blocked"
    assert _reasons(run) == {"quant_evaluate": "blocked by cycle_select (failed)"}
    assert len(fake.calls) == 8  # quant_evaluate was never started
    row = {r["step"]: r for r in _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_step")}[
        "quant_evaluate"
    ]
    assert row["status"] == "blocked"
    assert row["reason"] == "blocked by cycle_select (failed)"
    assert row["started_at"] is None


def test_quant_returns_failure_blocks_the_rest_of_quant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner({**_OK_ANSWERS, ("quant", "build-returns"): (1, "no backfill")})
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("pipeline", _ctx(tmp_path, news=news))
    statuses = _statuses(run)
    assert run.status == "failed"
    assert statuses["cycle_select"] == "completed"
    assert statuses["quant_actions"] == "completed"
    assert statuses["quant_returns"] == "failed"
    assert statuses["quant_risk_model"] == "blocked"
    assert statuses["quant_optimize"] == "blocked"
    assert statuses["quant_evaluate"] == "blocked"
    assert _reasons(run) == {
        "quant_risk_model": "blocked by quant_returns (failed)",
        "quant_optimize": "blocked by quant_risk_model (blocked)",
        "quant_evaluate": "blocked by quant_optimize (blocked)",
    }


def test_an_isolated_exception_fails_only_that_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner(_OK_ANSWERS, errors={("fundamental_agent", "run"): OSError("gateway died")})
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("pipeline", _ctx(tmp_path, news=news))
    statuses = _statuses(run)
    assert run.status == "failed"
    assert statuses["pricing"] == "completed"
    assert statuses["fundamental"] == "failed"
    assert statuses["entity_resolution"] == "completed"  # independent of fundamental
    assert statuses["cycle_select"] == "blocked"
    assert statuses["quant_actions"] == "blocked"
    fundamental = next(s for s in run.steps if s.name == "fundamental")
    assert fundamental.reason == "OSError: gateway died"
    # the same state is recorded (T-012)
    rows = {
        r["step"]: r
        for r in _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_step ORDER BY ordinal")
    }
    assert rows["fundamental"]["status"] == "failed"
    assert rows["fundamental"]["reason"] == "OSError: gateway died"
    assert rows["cycle_select"]["status"] == "blocked"
    assert rows["cycle_select"]["reason"] == "blocked by fundamental (failed)"


class _Boom(BaseException):
    """A BaseException that is not an Exception (T-013: the run must still stop)."""


def test_a_base_exception_from_a_step_stops_the_run_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    ctx = _ctx(tmp_path, news=news)
    fake = FakeRunner(_OK_ANSWERS, errors={("fundamental_agent", "run"): _Boom("boom")})
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.new_run("pipeline", ctx)
    with pytest.raises(_Boom):
        orch.execute_run(run, ctx)
    statuses = _statuses(run)
    assert run.status == "failed"
    assert statuses["pricing"] == "completed"
    assert statuses["fundamental"] == "failed"
    assert all(
        statuses[n] == "not_run"
        for n in (
            "entity_resolution",
            "cycle_select",
            "quant_actions",
            "quant_returns",
            "quant_risk_model",
            "quant_optimize",
            "quant_evaluate",
        )
    )
    fundamental = next(s for s in run.steps if s.name == "fundamental")
    assert fundamental.reason == "_Boom: boom"


def test_print_run_prints_every_failed_step_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner(
        {
            ("pricing_agent", "run"): (1, "pricing exploded"),
            ("fundamental_agent", "run"): (1, "fundamental exploded"),
            ("entity_resolution", "build"): (0, "cycle 3: 0 edges"),
        }
    )
    monkeypatch.setattr(orch, "run_command", fake)
    code = orch.cli_main(
        [
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
            "--news-db",
            str(news),
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "2 failed" in out
    assert "pricing failed (exit 1)" in out
    assert "fundamental failed (exit 1)" in out
    assert "pricing exploded" in out
    assert "fundamental exploded" in out


def test_keyboard_interrupt_records_the_state_then_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = _ctx(tmp_path)
    fake = FakeRunner(errors={("pricing_agent", "run"): KeyboardInterrupt()})
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.new_run("pipeline", ctx)
    with pytest.raises(KeyboardInterrupt):
        orch.execute_run(run, ctx)
    assert run.status == "failed"
    assert run.steps[0].status == "failed"
    assert run.steps[0].reason is not None and run.steps[0].reason.startswith("KeyboardInterrupt")
    assert all(s.status == "not_run" for s in run.steps[1:])


def test_missing_news_source_skips_entity_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner()
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("pipeline", _ctx(tmp_path))  # news path does not exist
    step = next(s for s in run.steps if s.name == "entity_resolution")
    assert step.status == "skipped"
    assert step.run_id is None
    assert step.reason is not None and step.reason.startswith("no news source configured")
    assert str(tmp_path / "missing_news.db") in step.reason
    assert run.status == "completed"
    # It was not launched: only the other 8 steps appear in the calls.
    assert not any(argv[2] == "entity_resolution" for argv in fake.argvs())


def test_present_news_source_runs_entity_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner({("entity_resolution", "build"): (0, "cycle 7: 0 edges")})
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("entity-resolution", _ctx(tmp_path, news=news))
    assert run.steps[0].status == "completed"
    assert run.steps[0].run_id == 7


# -- the CLI wrapper ----------------------------------------------------------------------------


def test_cli_wrapper_exit_zero_and_one_line_per_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    news = _touch(tmp_path / "news.db")
    answers = {
        ("pricing_agent", "run"): (0, "\nrun 1: ok"),
        ("fundamental_agent", "run"): (0, "\nrun 2: ok"),
        ("entity_resolution", "build"): (0, "cycle 3: 0 edges"),
        ("cycle", "select"): (0, "select 4 2026-10-02: ok"),
    }
    fake = FakeRunner(answers)
    monkeypatch.setattr(orch, "run_command", fake)
    code = orch.cli_main(
        [
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
            "--news-db",
            str(news),
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "pricing completed run_id=1" in out
    assert "entity_resolution completed run_id=3" in out
    assert len([line for line in out.splitlines() if " run_id=" in line]) == 9


def test_cli_wrapper_exit_one_and_prints_the_failing_tail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner({("fundamental_agent", "run"): (1, "gate refused: dq-v2 vs dq-v3")})
    monkeypatch.setattr(orch, "run_command", fake)
    code = orch.cli_main(
        [
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
            "--news-db",
            str(news),
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "fundamental failed" in out
    assert "gate refused: dq-v2 vs dq-v3" in out


def test_cli_prints_each_step_start_then_result_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    news = _touch(tmp_path / "news.db")
    answers = {
        ("pricing_agent", "run"): (0, "\nrun 1: ok"),
        ("fundamental_agent", "run"): (0, "\nrun 2: ok"),
        ("entity_resolution", "build"): (0, "cycle 3: 0 edges"),
        ("cycle", "select"): (0, "select 4 2026-10-02: ok"),
    }
    monkeypatch.setattr(orch, "run_command", FakeRunner(answers))
    code = orch.cli_main(
        [
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
            "--news-db",
            str(news),
        ]
    )
    assert code == 0
    lines = capsys.readouterr().out.splitlines()
    starts = [line for line in lines if line.endswith(" running ...")]
    assert starts == [
        f"{name} running ..."
        for name in (
            "pricing",
            "fundamental",
            "entity_resolution",
            "cycle_select",
            "quant_actions",
            "quant_returns",
            "quant_risk_model",
            "quant_optimize",
            "quant_evaluate",
        )
    ]

    def _result(name: str) -> int:
        return next(
            i for i, line in enumerate(lines) if line.startswith(f"{name} ") and " run_id=" in line
        )

    for name in ("pricing", "fundamental", "entity_resolution", "cycle_select"):
        assert lines.index(f"{name} running ...") < _result(name)


def test_cli_keyboard_interrupt_prints_the_state_and_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner(
        {("pricing_agent", "run"): (0, "\nrun 1: ok")},
        errors={("fundamental_agent", "run"): KeyboardInterrupt()},
    )
    monkeypatch.setattr(orch, "run_command", fake)
    with pytest.raises(KeyboardInterrupt):
        orch.cli_main(
            [
                "--analysis-date",
                "2026-10-02",
                "--db",
                str(tmp_path / "fin.db"),
                "--universe-db",
                str(tmp_path / "universe.db"),
                "--news-db",
                str(news),
            ]
        )
    out = capsys.readouterr().out
    assert "pricing running ..." in out
    assert "pricing completed run_id=1" in out
    assert "fundamental running ..." in out
    assert "fundamental failed" in out
    assert "pipeline: failed" in out


def test_cli_wrapper_uses_the_same_argv_as_the_http_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner()
    monkeypatch.setattr(orch, "run_command", fake)
    orch.cli_main(
        [
            "--analysis-date",
            "2026-10-02",
            "--top-n",
            "30",
            "--weight-scheme",
            "equal",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
            "--news-db",
            str(news),
        ]
    )
    expected = orch.new_run(
        "pipeline",
        _ctx(tmp_path, news=news, top_n=30, weight_scheme="equal"),
    ).steps
    assert [argv[1:] for argv in fake.argvs()] == [s.argv for s in expected]
    assert all(argv[0] == sys.executable for argv in fake.argvs())


def test_plain_python_m_api_still_starts_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}

    class _Settings:
        host, port, root_path, title = "h", 1, "", "t"

        @classmethod
        def load(cls) -> _Settings:
            return cls()

    def _cli(argv: list[str]) -> int:
        calls["cli"] = argv
        return 0

    monkeypatch.setattr(entry, "ApiSettings", _Settings)
    monkeypatch.setattr(entry.uvicorn, "run", lambda *a, **k: calls.setdefault("server", a))
    monkeypatch.setattr(entry, "cli_main", _cli)

    assert entry.main([]) == 0
    assert "server" in calls and "cli" not in calls
    calls.clear()
    assert entry.main(["orchestrate", "--analysis-date", "2026-10-02"]) == 0
    assert calls["cli"] == ["--analysis-date", "2026-10-02"]


# -- constants pinned to their owners -----------------------------------------------------------


def test_weight_schemes_match_cycles() -> None:
    assert orch.WEIGHT_SCHEMES == SCHEMES


def test_news_default_matches_entity_resolutions() -> None:
    assert DEFAULT_NEWS_DB == ER_DEFAULT


# -- provenance (T-012) -------------------------------------------------------------------------


def _fetch(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list:
    conn = kg_schema.connect_ro(db_path)
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _flaky_persist(fail_on: int) -> object:
    """A ``_persist_step`` that raises on its *fail_on*-th call (a mid-run UPDATE failure)."""
    real = orch._persist_step
    state = {"n": 0}

    def flaky(step: orch.StepState) -> None:
        state["n"] += 1
        if state["n"] == fail_on:
            raise orch._ProvenanceWriteFailed(OSError("disk full"))
        real(step)

    return flaky


def test_new_run_records_the_run_and_the_step_rows(tmp_path: Path) -> None:
    run = orch.new_run("pipeline", _ctx(tmp_path))
    runs = _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_run")
    assert len(runs) == 1
    assert runs[0]["handle"] == run.handle
    assert runs[0]["kind"] == "pipeline"
    assert runs[0]["as_of"] == "2026-10-02"
    assert runs[0]["surface"] == "http"
    assert runs[0]["status"] == "running"
    assert runs[0]["code_version"]
    steps = _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_step ORDER BY ordinal")
    assert [s["ordinal"] for s in steps] == list(range(1, 10))
    assert [s["step"] for s in steps] == [s.name for s in run.steps]
    assert all(s["status"] == "pending" for s in steps)
    assert [s["run_kind"] for s in steps] == [
        "pricing",
        "analysis",
        "cycle",
        "cycle",
        "quant",
        "quant",
        "quant",
        "quant",
        "quant",
    ]
    assert all(s["argv_json"] for s in steps)


def test_a_pipeline_run_records_each_steps_status_and_run_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    answers = {
        ("pricing_agent", "run"): (0, "\nrun 1: ok"),
        ("fundamental_agent", "run"): (0, "\nrun 2: ok"),
        ("entity_resolution", "build"): (0, "cycle 3: 0 edges"),
        ("cycle", "select"): (0, "select 4 2026-10-02: ok"),
    }
    monkeypatch.setattr(orch, "run_command", FakeRunner(answers))
    run = orch.run_now("pipeline", _ctx(tmp_path, news=news))
    assert run.status == "completed"
    rows = {
        r["step"]: r
        for r in _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_step ORDER BY ordinal")
    }
    for name, kind, run_id in (
        ("pricing", "pricing", 1),
        ("fundamental", "analysis", 2),
        ("entity_resolution", "cycle", 3),
        ("cycle_select", "cycle", 4),
    ):
        assert rows[name]["run_kind"] == kind
        assert rows[name]["run_id"] == run_id
        assert rows[name]["status"] == "completed"
        assert rows[name]["finished_at"]
    assert all(rows[n]["run_kind"] == "quant" for n in ("quant_actions", "quant_evaluate"))
    run_row = _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_run")[0]
    assert run_row["status"] == "completed"
    assert run_row["finished_at"] is not None


def test_a_single_step_run_records_one_run_and_one_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        orch, "run_command", FakeRunner({("pricing_agent", "run"): (0, "\nrun 1: ok")})
    )
    orch.run_now("pricing-agent", _ctx(tmp_path))
    runs = _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_run")
    steps = _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_step")
    assert len(runs) == 1 and len(steps) == 1
    assert runs[0]["kind"] == "pricing-agent"
    assert steps[0]["step"] == "pricing" and steps[0]["ordinal"] == 1


def test_a_failed_step_records_failed_and_its_dependents_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner(
        {
            ("pricing_agent", "run"): (0, "\nrun 1: ok"),
            ("fundamental_agent", "run"): (1, "boom"),
        }
    )
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("pipeline", _ctx(tmp_path, news=news))
    assert run.status == "failed"
    rows = {
        r["step"]: r
        for r in _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_step ORDER BY ordinal")
    }
    assert rows["pricing"]["status"] == "completed"
    assert rows["fundamental"]["status"] == "failed"
    assert rows["fundamental"]["exit_code"] == 1
    assert rows["entity_resolution"]["status"] == "completed"  # independent of fundamental
    for name in ("cycle_select", "quant_actions", "quant_evaluate"):
        assert rows[name]["status"] == "blocked"
    run_row = _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_run")[0]
    assert run_row["status"] == "failed" and run_row["finished_at"] is not None


def test_a_provenance_write_failure_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    news = _touch(tmp_path / "news.db")
    fake = FakeRunner({("pricing_agent", "run"): (0, "\nrun 1: ok")})
    monkeypatch.setattr(orch, "run_command", fake)
    monkeypatch.setattr(orch, "_persist_step", _flaky_persist(3))
    run = orch.run_now("pipeline", _ctx(tmp_path, news=news))
    assert run.status == "failed"
    failed = next(s for s in run.steps if s.status == "failed")
    assert failed.name == "pricing"
    assert failed.reason == "provenance write failed: OSError: disk full"
    assert all(s.status == "not_run" for s in run.steps[1:])
    assert len(fake.calls) == 1  # no further child started


def test_no_transaction_is_open_while_run_command_executes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes = {"n": 0}

    def fake(argv: list[str], env: dict[str, str]) -> orch.CommandResult:
        # If the orchestrator held a transaction, this second writer would block/fail.
        writes["n"] += 1
        conn = kg_schema.connect(tmp_path / "fin.db")
        try:
            conn.execute(
                "INSERT INTO universe_coverage "
                "(as_of, universe, symbol, in_assets, has_fundamental, has_metrics, has_pricing, "
                "has_observations, has_returns, covered, checked_at) "
                "VALUES ('2026-10-02', 'SP500', ?, 1, 0, 0, 0, 0, 0, 0, 'now')",
                (f"ZZ{writes['n']}",),
            )
            conn.commit()
        finally:
            conn.close()
        return orch.CommandResult(returncode=0, output="\nrun 1: ok")

    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now("pipeline", _ctx(tmp_path))
    assert run.status == "completed"
    # every child's own write landed: no transaction was held across a step
    assert (
        _fetch(tmp_path / "fin.db", "SELECT COUNT(*) AS n FROM universe_coverage")[0]["n"]
        == (writes["n"])
    )
    assert writes["n"] > 0


def test_cli_exits_1_when_the_run_row_cannot_be_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = FakeRunner()
    monkeypatch.setattr(orch, "run_command", fake)
    not_a_db = tmp_path / "not_a_db"
    not_a_db.mkdir()
    code = orch.cli_main(
        [
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(not_a_db),
            "--universe-db",
            str(tmp_path / "universe.db"),
            "--news-db",
            str(tmp_path / "news.db"),
        ]
    )
    assert code == 1
    assert fake.calls == []  # no child started
    assert "cannot record the run" in capsys.readouterr().err


def test_cli_prints_the_state_and_exits_1_on_a_provenance_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    news = _touch(tmp_path / "news.db")
    monkeypatch.setattr(
        orch, "run_command", FakeRunner({("pricing_agent", "run"): (0, "\nrun 1: ok")})
    )
    monkeypatch.setattr(orch, "_persist_step", _flaky_persist(3))
    code = orch.cli_main(
        [
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
            "--news-db",
            str(news),
        ]
    )
    out = capsys.readouterr().out
    assert code == 1
    assert "pipeline: failed" in out
    assert "provenance write failed" in out


# -- verify_pilot: T-013 (and T-012 on a multi-failure run) --------------------------------------

_ORCH_DDL = """
CREATE TABLE orchestrator_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT, handle TEXT, kind TEXT, as_of TEXT, surface TEXT,
    settings_json TEXT, status TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    code_version TEXT);
CREATE TABLE orchestrator_step (
    id INTEGER PRIMARY KEY AUTOINCREMENT, orchestrator_run_id INTEGER, ordinal INTEGER,
    step TEXT, argv_json TEXT, status TEXT NOT NULL, started_at TEXT, finished_at TEXT,
    exit_code INTEGER, run_kind TEXT, run_id INTEGER, reason TEXT, output_tail TEXT);
"""

_STEP_NAMES = (
    "pricing",
    "fundamental",
    "entity_resolution",
    "cycle_select",
    "quant_actions",
    "quant_returns",
    "quant_risk_model",
    "quant_optimize",
    "quant_evaluate",
)


def _write_orchestrator_db(path: Path, *, status: str, steps: dict[str, str]) -> None:
    """A raw pipeline run + steps, for the read-only verify_pilot checks."""
    conn = sqlite3.connect(path)
    conn.executescript(_ORCH_DDL)
    conn.execute(
        "INSERT INTO orchestrator_run (id, kind, status, started_at, finished_at) "
        "VALUES (1, 'pipeline', ?, '2026-10-02T00:00:00Z', '2026-10-02T00:01:00Z')",
        (status,),
    )
    for ordinal, name in enumerate(_STEP_NAMES, start=1):
        conn.execute(
            "INSERT INTO orchestrator_step (orchestrator_run_id, ordinal, step, argv_json, status) "
            "VALUES (1, ?, ?, '[]', ?)",
            (ordinal, name, steps.get(name, "not_run")),
        )
    conn.commit()
    conn.close()


def _verify_pilot_line(db_path: Path, task: str) -> str:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "scripts/verify_pilot.py", "--db", str(db_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    lines = [line for line in result.stdout.splitlines() if task in line]
    assert len(lines) == 1, f"expected one {task} line: {result.stdout} {result.stderr}"
    return lines[0]


def test_verify_pilot_t013_passes_an_isolated_run(tmp_path: Path) -> None:
    _write_orchestrator_db(
        tmp_path / "fin.db",
        status="failed",
        steps={
            "pricing": "failed",
            "fundamental": "completed",
            "entity_resolution": "skipped",
            "cycle_select": "blocked",
            "quant_actions": "blocked",
            "quant_returns": "blocked",
            "quant_risk_model": "blocked",
            "quant_optimize": "blocked",
            "quant_evaluate": "blocked",
        },
    )
    line = _verify_pilot_line(tmp_path / "fin.db", "T-013")
    assert "[PASS] T-013" in line
    assert "1 runs, 1 failed steps, 6 blocked steps" in line


def test_verify_pilot_t013_fails_a_completed_step_with_a_failed_upstream(tmp_path: Path) -> None:
    _write_orchestrator_db(
        tmp_path / "fin.db",
        status="failed",
        steps={
            "pricing": "failed",
            "fundamental": "completed",
            "entity_resolution": "skipped",
            "cycle_select": "completed",
        },
    )
    line = _verify_pilot_line(tmp_path / "fin.db", "T-013")
    assert "[FAIL] T-013" in line
    assert "cycle_select is completed with a failed/blocked upstream" in line


def test_verify_pilot_t013_fails_a_blocked_step_with_completed_upstreams(tmp_path: Path) -> None:
    _write_orchestrator_db(
        tmp_path / "fin.db",
        status="failed",
        steps={
            "pricing": "completed",
            "fundamental": "completed",
            "entity_resolution": "skipped",
            "cycle_select": "blocked",
        },
    )
    line = _verify_pilot_line(tmp_path / "fin.db", "T-013")
    assert "[FAIL] T-013" in line
    assert "cycle_select is blocked with no failed/blocked upstream" in line


def test_verify_pilot_t013_fails_a_completed_run_with_a_blocked_step(tmp_path: Path) -> None:
    _write_orchestrator_db(
        tmp_path / "fin.db",
        status="completed",
        steps={
            "pricing": "failed",
            "fundamental": "completed",
            "entity_resolution": "skipped",
            "cycle_select": "blocked",
        },
    )
    line = _verify_pilot_line(tmp_path / "fin.db", "T-013")
    assert "[FAIL] T-013" in line
    assert "completed with a blocked step" in line


def test_verify_pilot_t012_passes_a_failed_run_with_two_failed_steps(tmp_path: Path) -> None:
    _write_orchestrator_db(
        tmp_path / "fin.db",
        status="failed",
        steps={
            "pricing": "failed",
            "fundamental": "failed",
            "cycle_select": "blocked",
            "quant_actions": "blocked",
            "quant_returns": "blocked",
            "quant_risk_model": "blocked",
            "quant_optimize": "blocked",
            "quant_evaluate": "blocked",
        },
    )
    assert "[PASS] T-012" in _verify_pilot_line(tmp_path / "fin.db", "T-012")
