"""The incremental, upstream-aware run (T-017): which steps run, what is recorded, how the
checks are built. Hermetic: ``run_command`` is replaced, no agent or gateway is ever spawned."""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest
from test_orchestrator import (
    _ORCH_DDL,
    _STEP_NAMES,
    FakeRunner,
    _ctx,
    _touch,
    _verify_pilot_line,
)

from api import orchestrator as orch

_OUT = {
    ("pricing_agent", "run"): (0, "run 1: 5 priced\n"),
    ("fundamental_agent", "run"): (0, "run 1: 0 analyzed\n"),
    ("entity_resolution", "build"): (0, "cycle 1: 0 edges\n"),
    ("cycle", "select"): (0, "select 1 done\n"),
}
_NO = "pending: no nothing new"
_YES = "pending: yes 3 new"
_QUANT = ("quant_actions", "quant_returns", "quant_risk_model", "quant_optimize", "quant_evaluate")


def _run(  # noqa: PLR0913 - the fake's three answer tables, all optional
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    checks: Mapping[str, tuple[int, str] | BaseException],
    *,
    news: bool = True,
    errors: dict[tuple[str, str], BaseException] | None = None,
    answers: dict[tuple[str, str], tuple[int, str]] | None = None,
) -> tuple[orch.RunState, FakeRunner]:
    fake = FakeRunner({**_OUT, **(answers or {})}, errors, checks)
    monkeypatch.setattr(orch, "run_command", fake)
    paths_news = _touch(tmp_path / "urls.db") if news else None
    run = orch.run_now("pipeline", _ctx(tmp_path, news=paths_news))
    return run, fake


def _by_name(run: orch.RunState) -> dict[str, orch.StepState]:
    return {s.name: s for s in run.steps}


def _nothing_new() -> dict[str, tuple[int, str]]:
    return {
        pkg: (0, _NO)
        for pkg in ("pricing_agent", "fundamental_agent", "entity_resolution", "quant")
    }


# -- which steps run ----------------------------------------------------------------------------


def test_nothing_pending_runs_only_cycle_select(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, fake = _run(tmp_path, monkeypatch, _nothing_new())
    steps = _by_name(run)
    assert [a[2:4] for a, _ in fake.calls] == [["cycle", "select"]]
    assert run.status == "completed"
    assert steps["cycle_select"].status == "completed"
    assert steps["cycle_select"].reason == f"always runs; {orch._SEMANTIC_NOTE}"
    for name in ("pricing", "fundamental", "entity_resolution"):
        assert steps[name].status == "skipped"
        assert steps[name].reason == "not pending: nothing new"
    assert steps["quant_actions"].reason == "not pending: nothing new"
    for name in _QUANT[1:]:
        assert steps[name].status == "skipped"
        assert str(steps[name].reason).startswith("not pending:")
    # quant_evaluate: cycle_select ran, but its always-run is not a pending signal
    assert "cycle_select completed" in (steps["quant_evaluate"].reason or "")


def test_pricing_pending_runs_cycle_select_and_the_quant_chain_as_dependents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = {**_nothing_new(), "pricing_agent": (0, "pending: yes 5 members lack 2026-10-09")}
    run, fake = _run(tmp_path, monkeypatch, checks)
    steps = _by_name(run)
    assert steps["pricing"].reason == "pending: 5 members lack 2026-10-09"
    assert steps["fundamental"].status == "skipped"
    assert steps["cycle_select"].reason == f"pending upstream: pricing; {orch._SEMANTIC_NOTE}"
    assert steps["quant_actions"].reason.startswith("pending upstream: pricing")  # type: ignore[union-attr]
    assert steps["quant_returns"].reason.startswith("pending upstream: quant_actions")  # type: ignore[union-attr]
    assert steps["quant_evaluate"].reason.startswith("pending upstream: quant_optimize")  # type: ignore[union-attr]
    assert all(steps[n].status == "completed" for n in ("pricing", "cycle_select", *_QUANT))
    # a dependent that runs because of an upstream runs no check of its own
    assert {a[2] for a, _, _ in fake.check_calls} == {
        "pricing_agent",
        "fundamental_agent",
        "entity_resolution",
    }


def test_fundamental_pending_runs_cycle_select_and_the_quant_chain_as_dependents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = {**_nothing_new(), "fundamental_agent": (0, "pending: yes 1 unscored filing")}
    run, _ = _run(tmp_path, monkeypatch, checks)
    steps = _by_name(run)
    assert steps["pricing"].status == "skipped"
    assert steps["cycle_select"].reason.startswith("pending upstream: fundamental")  # type: ignore[union-attr]
    assert steps["quant_actions"].reason.startswith("pending upstream: fundamental")  # type: ignore[union-attr]
    assert all(steps[n].status == "completed" for n in ("fundamental", "cycle_select", *_QUANT))


def test_only_quant_actions_pending_runs_the_chain_and_cycle_select_always(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = {**_nothing_new(), "quant": (0, "pending: yes MA has DIVIDEND 2026-10-09")}
    run, _ = _run(tmp_path, monkeypatch, checks)
    steps = _by_name(run)
    assert steps["quant_actions"].reason.startswith("pending: MA has DIVIDEND")  # type: ignore[union-attr]
    assert steps["quant_returns"].reason.startswith("pending upstream: quant_actions")  # type: ignore[union-attr]
    assert steps["cycle_select"].reason == f"always runs; {orch._SEMANTIC_NOTE}"
    assert steps["pricing"].status == steps["fundamental"].status == "skipped"
    assert all(steps[n].status == "completed" for n in _QUANT)


def test_cycle_select_never_enters_ran_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keyed on the ``always_run`` flag: even as a dependent, cycle_select's run is not a pending
    signal, so it cannot make quant_evaluate (which depends on it) pending."""
    fake = FakeRunner(_OUT, None, _nothing_new())
    monkeypatch.setattr(orch, "run_command", fake)
    ctx = _ctx(tmp_path)
    run = orch.new_run("pipeline", ctx)
    states = _by_name(run)
    specs = {s.name: s for s in orch.steps_for("pipeline")}
    progress = orch._Progress()
    progress.ran_pending.add("pricing")
    progress.status_of.update({"quant_optimize": "skipped"})
    orch._execute_step(specs["cycle_select"], states["cycle_select"], ctx, progress, None)
    assert states["cycle_select"].reason.startswith("pending upstream: pricing")  # type: ignore[union-attr]
    assert "cycle_select" not in progress.ran_pending
    orch._execute_step(specs["quant_evaluate"], states["quant_evaluate"], ctx, progress, None)
    assert states["quant_evaluate"].status == "skipped"
    assert (states["quant_evaluate"].reason or "").startswith("not pending:")


# -- a check that fails -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "cause"),
    [
        ((2, "Traceback ...\nboom\n"), "exit 2: boom"),
        ((0, "no marker here\n"), "unparseable output (0 pending lines)"),
        ((0, "pending: yes a\npending: no b\n"), "unparseable output (2 pending lines)"),
        (subprocess.TimeoutExpired(["x"], 1800), "timeout after 1800 s"),
        (FileNotFoundError("no python"), "FileNotFoundError: no python"),
    ],
)
def test_a_failed_check_counts_as_pending_and_never_fails_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answer: tuple[int, str] | BaseException,
    cause: str,
) -> None:
    checks = {**_nothing_new(), "pricing_agent": answer}
    run, _ = _run(tmp_path, monkeypatch, checks)
    steps = _by_name(run)
    assert steps["pricing"].status == "completed"
    assert steps["pricing"].reason == f"pending check failed: {cause}"
    assert run.status == "completed"
    assert steps["cycle_select"].reason.startswith("pending upstream: pricing")  # type: ignore[union-attr]


def test_a_pending_step_that_fails_blocks_its_dependents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    checks = {**_nothing_new(), "pricing_agent": (0, _YES)}
    run, fake = _run(
        tmp_path, monkeypatch, checks, answers={("pricing_agent", "run"): (1, "boom\n")}
    )
    steps = _by_name(run)
    assert steps["pricing"].status == "failed"
    assert steps["pricing"].reason.startswith("pending: 3 new")  # type: ignore[union-attr]  # kept
    assert steps["cycle_select"].status == "blocked"
    assert steps["quant_actions"].status == "blocked"
    assert steps["quant_evaluate"].status == "blocked"
    assert steps["fundamental"].status == "skipped"
    assert run.status == "failed"
    # a blocked step runs no check: quant never asked
    assert "quant" not in {a[2] for a, _, _ in fake.check_calls}


# -- unchanged behaviour ------------------------------------------------------------------------


def test_the_no_news_skip_is_unchanged_and_runs_no_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run, fake = _run(tmp_path, monkeypatch, _nothing_new(), news=False)
    step = _by_name(run)["entity_resolution"]
    assert step.status == "skipped"
    assert (step.reason or "").startswith("no news source configured (")
    assert "entity_resolution" not in {a[2] for a, _, _ in fake.check_calls}


@pytest.mark.parametrize("kind", ["pricing-agent", "quant-backfill-actions", "cycle-select"])
def test_single_step_kinds_run_no_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    fake = FakeRunner(_OUT, None, {})
    monkeypatch.setattr(orch, "run_command", fake)
    run = orch.run_now(kind, _ctx(tmp_path))
    assert fake.check_calls == []
    assert [s.status for s in run.steps] == ["completed"]
    assert not any(
        (s.reason or "").startswith(("pending", "not pending", "always")) for s in run.steps
    )


def test_check_argv_is_built_from_literals(tmp_path: Path) -> None:
    news = _touch(tmp_path / "urls.db")
    ctx = _ctx(tmp_path, news=news, top_n=7, weight_scheme="equal", max_name_weight=0.1)
    for spec in orch.steps_for("pipeline"):
        if spec.pending_command is None:
            continue
        argv = orch.build_check_argv(spec, ctx)
        expected = [
            sys.executable,
            "-m",
            spec.package,
            "pending",
            "--analysis-date",
            "2026-10-02",
            "--db",
            str(tmp_path / "fin.db"),
            "--universe-db",
            str(tmp_path / "universe.db"),
        ]
        if spec.with_news_db:
            expected += ["--news-db", str(news)]
        assert argv == expected  # no construction setting, no --allow-*, no migrate
    assert {s.name for s in orch.steps_for("pipeline") if s.pending_command} == {
        "pricing",
        "fundamental",
        "entity_resolution",
        "quant_actions",
    }
    assert [s.name for s in orch.steps_for("pipeline") if s.always_run] == ["cycle_select"]


def test_checks_get_the_timeout_and_steps_do_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, fake = _run(tmp_path, monkeypatch, _nothing_new())
    assert {t for _, _, t in fake.check_calls} == {1800}  # FakeRunner asserts steps get none


def test_run_command_kills_a_check_that_outlives_its_timeout() -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        orch.run_command([sys.executable, "-c", "import time; time.sleep(30)"], {}, timeout=0.5)


def test_the_decision_is_recorded_on_the_step_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, monkeypatch, _nothing_new())
    conn = sqlite3.connect(tmp_path / "fin.db")
    rows = dict(
        conn.execute("SELECT step, status || ' | ' || COALESCE(reason, '') FROM orchestrator_step")
    )
    assert rows["pricing"] == "skipped | not pending: nothing new"
    assert rows["cycle_select"].startswith("completed | always runs")


# -- verify_pilot T-017 -------------------------------------------------------------------------


def _write_run(path: Path, steps: dict[str, tuple[str, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_ORCH_DDL)
    conn.execute(
        "INSERT INTO orchestrator_run (id, kind, status, started_at, finished_at) "
        "VALUES (1, 'pipeline', 'completed', '2026-10-02T00:00:00Z', '2026-10-02T00:01:00Z')"
    )
    for ordinal, name in enumerate(_STEP_NAMES, start=1):
        status, reason = steps.get(name, ("completed", ""))
        conn.execute(
            "INSERT INTO orchestrator_step (orchestrator_run_id, ordinal, step, argv_json, "
            "status, reason) VALUES (1, ?, ?, '[]', ?, ?)",
            (ordinal, name, status, reason or None),
        )
    conn.commit()
    conn.close()


_CLEAN = {
    "pricing": ("skipped", "not pending: 5/5 members at 2026-10-02"),
    "fundamental": ("skipped", "not pending: 5 members current"),
    "entity_resolution": ("skipped", "no news source configured (/x not found)"),
    "cycle_select": ("completed", "always runs; semantic: no semantic source configured"),
    "quant_actions": ("skipped", "not pending: returns current"),
    "quant_returns": (
        "skipped",
        "not pending: no upstream ran on a pending signal (quant_actions skipped)",
    ),
    "quant_risk_model": ("skipped", "not pending: no upstream ran on a pending signal (x)"),
    "quant_optimize": ("skipped", "not pending: no upstream ran on a pending signal (x)"),
    "quant_evaluate": ("skipped", "not pending: no upstream ran on a pending signal (x)"),
}


def test_verify_pilot_t017_passes_a_clean_incremental_run(tmp_path: Path) -> None:
    _write_run(tmp_path / "fin.db", _CLEAN)
    line = _verify_pilot_line(tmp_path / "fin.db", "T-017")
    assert "[PASS] T-017" in line and "1 runs, 8 skipped steps" in line


def test_verify_pilot_t017_passes_a_run_recorded_before_t017(tmp_path: Path) -> None:
    _write_run(
        tmp_path / "fin.db",
        {"entity_resolution": ("skipped", "no news source configured (/x not found)")},
    )
    assert "[PASS] T-017" in _verify_pilot_line(tmp_path / "fin.db", "T-017")


def test_verify_pilot_t017_fails_rule_i_a_skip_under_an_upstream_that_ran_pending(
    tmp_path: Path,
) -> None:
    steps = {
        **_CLEAN,
        "pricing": ("completed", "pending: 5 members lack 2026-10-09"),
    }
    _write_run(tmp_path / "fin.db", steps)  # quant_actions depends on pricing, yet skipped
    line = _verify_pilot_line(tmp_path / "fin.db", "T-017")
    assert "[FAIL] T-017" in line
    assert "quant_actions skipped not pending with an upstream that ran pending" in line


def test_verify_pilot_t017_does_not_count_cycle_select_as_a_pending_upstream(
    tmp_path: Path,
) -> None:
    # cycle_select ran as a dependent of a skipped-looking pricing (inconsistent on purpose);
    # quant_evaluate (depends on quant_optimize + cycle_select) is skipped, quant_optimize too
    steps = {**_CLEAN, "cycle_select": ("completed", "pending upstream: pricing")}
    _write_run(tmp_path / "fin.db", steps)
    assert "[PASS] T-017" in _verify_pilot_line(tmp_path / "fin.db", "T-017")


def test_verify_pilot_t017_fails_a_skipped_evaluate_under_an_optimize_that_ran_pending(
    tmp_path: Path,
) -> None:
    steps = {
        **_CLEAN,
        "quant_optimize": ("completed", "pending upstream: quant_risk_model"),
    }
    _write_run(tmp_path / "fin.db", steps)
    line = _verify_pilot_line(tmp_path / "fin.db", "T-017")
    assert "[FAIL] T-017" in line and "quant_evaluate skipped not pending" in line


def test_verify_pilot_t017_fails_rule_ii_cycle_select_skipped_not_pending(tmp_path: Path) -> None:
    _write_run(
        tmp_path / "fin.db",
        {**_CLEAN, "cycle_select": ("skipped", "not pending: nothing new")},
    )
    line = _verify_pilot_line(tmp_path / "fin.db", "T-017")
    assert "[FAIL] T-017" in line and "cycle_select is always-run but skipped not pending" in line


def test_verify_pilot_t017_fails_rule_iii_an_unexplained_skip(tmp_path: Path) -> None:
    _write_run(
        tmp_path / "fin.db",
        {**_CLEAN, "fundamental": ("skipped", "because")},
    )
    line = _verify_pilot_line(tmp_path / "fin.db", "T-017")
    assert "[FAIL] T-017" in line and "fundamental skipped without a recognised reason" in line


def test_verify_pilot_t017_skips_a_database_with_no_pipeline_run(tmp_path: Path) -> None:
    conn = sqlite3.connect(tmp_path / "fin.db")
    conn.executescript(_ORCH_DDL)
    conn.commit()
    conn.close()
    assert "[SKIP] T-017" in _verify_pilot_line(tmp_path / "fin.db", "T-017")
