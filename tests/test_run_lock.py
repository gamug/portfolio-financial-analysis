"""The run lock, abandoned-run reconcile and graceful stops (T-015).

Hermetic: ``run_command`` is replaced except where a test proves the real ``Popen`` seam (a tiny
``python -c`` child, no agent and no network) -- there, and in the orphan test, a real child is
started on purpose. The lock file lives under ``tmp_path``.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import kg_schema
from api import orchestrator as orch
from api.app import create_app
from api.config import ApiSettings

_DATE = "2026-10-02"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    orch.clear_runs()  # also releases the lock and clears the stop flag
    yield
    orch.clear_runs()


def _ctx(tmp_path: Path) -> orch.RunContext:
    return orch.RunContext(
        paths=orch.RunPaths(
            db_path=tmp_path / "fin.db",
            universe_db_path=tmp_path / "universe.db",
            news_db_path=tmp_path / "missing_news.db",
        ),
        analysis_date=_DATE,
    )


def _cli_argv(tmp_path: Path, db: Path | str | None = None) -> list[str]:
    return [
        "--analysis-date",
        _DATE,
        "--db",
        str(db if db is not None else tmp_path / "fin.db"),
        "--universe-db",
        str(tmp_path / "universe.db"),
        "--news-db",
        str(tmp_path / "missing_news.db"),
    ]


class FakeRunner:
    """A ``run_command`` that answers every step 0 and every check ``pending: yes``. Records the
    step calls in ``calls``; a ``gate`` blocks each step until it is set, and a stop requested
    while gated makes the next call raise :class:`RunStopped` (the real seam's own behaviour)."""

    def __init__(self, gate: threading.Event | None = None) -> None:
        self.gate = gate
        self.calls: list[list[str]] = []
        self.checks: list[list[str]] = []
        self.started = threading.Event()

    def __call__(
        self, argv: list[str], env: dict[str, str], timeout: float | None = None
    ) -> orch.CommandResult:
        if argv[3] == "pending":
            self.checks.append(list(argv))
            return orch.CommandResult(returncode=0, output="pending: yes test")
        self.calls.append(list(argv))
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(timeout=10)
        stop = orch._stop_reason()
        if stop is not None:
            raise orch.RunStopped(stop)
        return orch.CommandResult(returncode=0, output="\nrun 1: ok")


def _fetch(db_path: Path, sql: str, params: tuple[object, ...] = ()) -> list:
    conn = kg_schema.connect_ro(db_path)
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _seed_run(db_path: Path, *, status: str, steps: dict[str, str], handle: str) -> int:
    """A raw orchestrator run + steps, to be reconciled by the next run."""
    orch._ensure_tables(db_path)
    conn = kg_schema.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO orchestrator_run "
            "(handle, kind, as_of, surface, settings_json, status, started_at, code_version) "
            "VALUES (?, 'pipeline', ?, 'cli', NULL, ?, ?, 'test')",
            (handle, _DATE, status, "2026-10-02T00:00:00+00:00"),
        )
        run_pk = int(cur.lastrowid or 0)
        for ordinal, (name, step_status) in enumerate(steps.items(), start=1):
            conn.execute(
                "INSERT INTO orchestrator_step "
                "(orchestrator_run_id, ordinal, step, argv_json, status) VALUES (?, ?, ?, '[]', ?)",
                (run_pk, ordinal, name, step_status),
            )
        conn.commit()
    finally:
        conn.close()
    return run_pk


# -- the lock -----------------------------------------------------------------------------------


def test_the_lock_is_held_during_a_run_and_free_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner())
    db = tmp_path / "fin.db"
    held: list[bool] = []
    identities: list[dict[str, Any]] = []

    def on_change(state: orch.StepState) -> None:
        if state.status == "running":
            held.append(orch._LOCK_STATE.fd is not None)
            identities.append(json.loads(orch.lock_path(db).read_text()))

    run = orch.run_now("pipeline", _ctx(tmp_path), on_step_change=on_change)
    assert held and all(held)
    assert orch._LOCK_STATE.fd is None  # released when the run finished
    assert identities[0]["handle"] == run.handle
    assert identities[0]["surface"] == "cli"
    assert "pid" in identities[0] and "started_at" in identities[0]
    assert orch.lock_path(db).exists()  # the file is never deleted


def test_a_cli_run_is_refused_while_the_lock_is_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner())
    db = tmp_path / "fin.db"
    holder = {"handle": "holder", "pid": 1, "started_at": "t", "surface": "http"}
    orch._acquire_lock(db, holder)
    try:
        code = orch.cli_main(_cli_argv(tmp_path))
    finally:
        orch._release_lock()
    assert code == 1
    assert orch.RunLocked.MESSAGE in capsys.readouterr().err
    assert not db.exists()  # a refused run starts nothing and writes no row


def test_http_run_is_409_with_the_holder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner())
    db = tmp_path / "fin.db"
    holder = {"handle": "holder", "pid": 12345, "started_at": "t", "surface": "cli"}
    settings = ApiSettings(
        db_path=db, universe_db_path=tmp_path / "universe.db", news_db_path=tmp_path / "news.db"
    )
    orch._acquire_lock(db, holder)
    try:
        with TestClient(create_app(settings)) as client:
            r = client.post("/api/v1/run/pipeline", json={"analysis_date": _DATE})
    finally:
        orch._release_lock()
    assert r.status_code == 409
    assert r.json()["detail"] == orch.RunLocked.MESSAGE
    assert r.json()["holder"] == holder
    assert not db.exists()


def test_an_orphaned_child_keeps_the_lock_until_it_exits(tmp_path: Path) -> None:
    db = tmp_path / "fin.db"
    fd = os.open(orch.lock_path(db), os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # the child inherits the fd (and so the open file description, and so the lock)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(2)"], pass_fds=(fd,))
    os.close(fd)  # the parent's own fd is gone; only the orphan holds the lock now
    try:
        with pytest.raises(orch.RunLocked):
            orch._acquire_lock(db, {"handle": "probe"})
    finally:
        child.wait()
    orch._acquire_lock(db, {"handle": "probe"})  # free once the orphan exited
    orch._release_lock()


# -- reconcile ----------------------------------------------------------------------------------


def test_reconcile_turns_a_stale_run_into_abandoned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner())
    db = tmp_path / "fin.db"
    stale = _seed_run(
        db,
        status="running",
        steps={"pricing": "running", "fundamental": "pending", "cycle_select": "pending"},
        handle="stale",
    )
    # a completed run must never be touched
    done = _seed_run(db, status="completed", steps={"pricing": "completed"}, handle="done")

    run, reconciled = orch.begin_run("pipeline", _ctx(tmp_path))
    try:
        assert reconciled == (1, 3)  # the stale run, its 1 running + 2 pending steps
    finally:
        orch._release_lock()

    rows = {r["run_id"]: r for r in _fetch(db, "SELECT * FROM v_orchestrator_run")}
    assert rows[stale]["status"] == "abandoned"
    assert rows[stale]["finished_at"] is not None
    assert rows[done]["status"] == "completed"
    assert rows[run.pk]["status"] == "running"

    steps = {
        r["step"]: r for r in _fetch(db, "SELECT * FROM v_orchestrator_step WHERE handle = 'stale'")
    }
    assert steps["pricing"]["status"] == "abandoned"
    assert steps["pricing"]["finished_at"] is not None
    assert steps["pricing"]["reason"] == (
        f"the orchestrator process ended while this step ran (reconciled by run {run.handle})"
    )
    assert steps["fundamental"]["status"] == "not_run"
    assert steps["cycle_select"]["status"] == "not_run"


def test_reconcile_happens_only_under_the_lock(tmp_path: Path) -> None:
    db = tmp_path / "fin.db"
    stale = _seed_run(db, status="running", steps={"pricing": "running"}, handle="stale")
    # new_run alone (no lock) records a run but reconciles nothing
    orch.new_run("pipeline", _ctx(tmp_path))
    assert (
        _fetch(db, "SELECT status FROM orchestrator_run WHERE id = ?", (stale,))[0]["status"]
        == "running"
    )
    # begin_run (holding the lock) reconciles it
    orch.begin_run("pipeline", _ctx(tmp_path))
    orch._release_lock()
    assert (
        _fetch(db, "SELECT status FROM orchestrator_run WHERE id = ?", (stale,))[0]["status"]
        == "abandoned"
    )


# -- releasing the lock on every failure path ----------------------------------------------------


def test_a_run_that_cannot_write_releases_the_lock_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner())
    not_a_db = tmp_path / "not_a_db"
    not_a_db.mkdir()
    code = orch.cli_main(_cli_argv(tmp_path, db=not_a_db))
    assert code == 1
    assert "cannot record the run" in capsys.readouterr().err
    assert orch._LOCK_STATE.fd is None  # released before the CLI returned
    orch._acquire_lock(not_a_db, {"handle": "probe"})  # so the next run can take it at once
    orch._release_lock()


def test_a_run_that_cannot_write_releases_the_lock_http(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(orch, "run_command", FakeRunner())
    not_a_db = tmp_path / "not_a_db"
    not_a_db.mkdir()
    settings = ApiSettings(
        db_path=not_a_db,
        universe_db_path=tmp_path / "universe.db",
        news_db_path=tmp_path / "news.db",
    )
    with TestClient(create_app(settings)) as client:
        r = client.post("/api/v1/run/pipeline", json={"analysis_date": _DATE})
    assert r.status_code == 500
    assert orch._LOCK_STATE.fd is None
    orch._acquire_lock(not_a_db, {"handle": "probe"})
    orch._release_lock()


# -- graceful stops ------------------------------------------------------------------------------


def test_the_sigterm_handler_raises_a_stop() -> None:
    with pytest.raises(orch.RunStopped) as excinfo:
        orch._on_sigterm(signal.SIGTERM, None)
    assert excinfo.value.reason == "stopped: SIGTERM"


def test_cli_sigterm_records_the_step_failed_and_the_rest_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def stopping(argv: list[str], env: dict[str, str], timeout: float | None = None) -> Any:
        if argv[3] == "pending":  # the check is fine; the stop lands on the step itself
            return orch.CommandResult(returncode=0, output="pending: yes test")
        raise orch.RunStopped("stopped: SIGTERM")  # what the handler raising does mid-step

    monkeypatch.setattr(orch, "run_command", stopping)
    code = orch.cli_main(_cli_argv(tmp_path))
    assert code == 1
    rows = {r["step"]: r for r in _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_step")}
    assert rows["pricing"]["status"] == "failed"
    assert rows["pricing"]["reason"] == "stopped: SIGTERM"
    assert all(
        rows[name]["status"] == "not_run"
        for name in ("fundamental", "entity_resolution", "cycle_select", "quant_evaluate")
    )
    run = _fetch(tmp_path / "fin.db", "SELECT * FROM v_orchestrator_run")[0]
    assert run["status"] == "failed"


def test_api_shutdown_records_the_stop_and_starts_no_later_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = threading.Event()
    fake = FakeRunner(gate=gate)
    monkeypatch.setattr(orch, "run_command", fake)
    settings = ApiSettings(
        db_path=tmp_path / "fin.db",
        universe_db_path=tmp_path / "universe.db",
        news_db_path=tmp_path / "news.db",
    )
    with TestClient(create_app(settings)) as client:
        handle = client.post("/api/v1/run/pipeline", json={"analysis_date": _DATE}).json()["handle"]
        assert fake.started.wait(timeout=10)
        # the shutdown hook sets the stop and waits for the runner; release the (gated) child
        # once the stop is set, so the join returns as soon as the runner records
        shutdown = threading.Thread(target=orch.shutdown_active_run)
        shutdown.start()
        for _ in range(200):
            if orch._stop_reason() is not None:
                break
            time.sleep(0.01)
        gate.set()
        shutdown.join(timeout=10)
        for _ in range(200):
            body = client.get(f"/api/v1/run/{handle}").json()
            if body["status"] in ("failed", "completed"):
                break
            time.sleep(0.01)
    assert body["status"] == "failed"
    pricing = next(s for s in body["steps"] if s["name"] == "pricing")
    assert pricing["status"] == "failed"
    assert pricing["reason"] == "stopped: API shutdown"
    assert all(s["status"] == "not_run" for s in body["steps"] if s["name"] != "pricing")
    assert len(fake.calls) == 1  # T-013 would have continued; the stop did not
    assert orch._LOCK_STATE.fd is None


def test_request_stop_kills_the_registered_child(tmp_path: Path) -> None:
    marker = tmp_path / "child_finished"
    with pytest.raises(orch.RunStopped) as excinfo:
        threading.Thread(target=_stop_soon, daemon=True).start()
        orch.run_command(
            [
                sys.executable,
                "-c",
                f"import time; time.sleep(3); open({str(marker)!r}, 'w').close()",
            ],
            dict(os.environ),
        )
    assert excinfo.value.reason == "stopped: test"
    time.sleep(0.5)
    assert not marker.exists()  # the child was killed, it never reached the end


def _stop_soon() -> None:
    time.sleep(0.4)
    orch.request_stop("stopped: test")


def test_run_command_refuses_to_spawn_after_a_stop() -> None:
    orch.request_stop("stopped: test")
    with pytest.raises(orch.RunStopped) as excinfo:
        orch.run_command([sys.executable, "-c", "import sys; sys.exit(0)"], {})
    assert excinfo.value.reason == "stopped: test"


# -- the Popen seam keeps today's behaviour ------------------------------------------------------


def test_run_command_timeout_kills_the_child(tmp_path: Path) -> None:
    marker = tmp_path / "child_finished"
    with pytest.raises(subprocess.TimeoutExpired):
        orch.run_command(
            [
                sys.executable,
                "-c",
                f"import time; time.sleep(3); open({str(marker)!r}, 'w').close()",
            ],
            dict(os.environ),
            timeout=0.3,
        )
    time.sleep(0.5)
    assert not marker.exists()


def test_run_command_kills_the_child_when_interrupted(tmp_path: Path) -> None:
    marker = tmp_path / "child_finished"

    def on_alarm(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, 0.3)
    try:
        with pytest.raises(KeyboardInterrupt):
            orch.run_command(
                [
                    sys.executable,
                    "-c",
                    f"import time; time.sleep(3); open({str(marker)!r}, 'w').close()",
                ],
                dict(os.environ),
            )
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    time.sleep(0.5)
    assert not marker.exists()


# -- a stop during a pending check, and between two steps (correction 1) --------------------------


def test_a_stop_while_a_pending_check_runs_records_the_step_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    check_started = threading.Event()
    release = threading.Event()

    def stopping_check(
        argv: list[str], env: dict[str, str], timeout: float | None = None
    ) -> orch.CommandResult:
        assert argv[3] == "pending"
        check_started.set()
        assert release.wait(timeout=10)
        stop = orch._stop_reason()
        if stop is not None:  # a killed check is a stop, never "failed -> pending"
            raise orch.RunStopped(stop)
        return orch.CommandResult(returncode=0, output="pending: yes test")

    monkeypatch.setattr(orch, "run_command", stopping_check)
    ctx = _ctx(tmp_path)
    run = orch.new_run("pipeline", ctx)

    def _execute() -> None:
        with contextlib.suppress(orch.RunStopped):  # execute_run re-raises the stop (BaseException)
            orch.execute_run(run, ctx)

    runner = threading.Thread(target=_execute)
    runner.start()
    assert check_started.wait(timeout=10)
    orch.request_stop("stopped: test")
    release.set()
    runner.join(timeout=10)
    assert not runner.is_alive()
    assert run.status == "failed"
    assert [s.status for s in run.steps] == ["not_run"] * 9  # pricing never ran, nor any later step


def test_a_stop_between_two_steps_starts_no_further_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRunner()
    monkeypatch.setattr(orch, "run_command", fake)
    stopped: list[str] = []

    def on_change(state: orch.StepState) -> None:
        if state.name == "pricing" and state.status == "completed" and not stopped:
            stopped.append(state.name)
            orch.request_stop("stopped: test")

    ctx = _ctx(tmp_path)
    run = orch.new_run("pipeline", ctx)
    orch.execute_run(run, ctx, on_step_change=on_change)
    statuses = {s.name: s.status for s in run.steps}
    assert statuses["pricing"] == "completed"
    assert all(v == "not_run" for k, v in statuses.items() if k != "pricing")
    assert run.status == "failed"
    assert len(fake.calls) == 1  # no further step child was spawned


# -- the verify_pilot T-015 check ----------------------------------------------------------------

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


def _write_orch_db(
    path: Path,
    runs: list[tuple[int, str, str | None]],
    steps: list[tuple[int, str, str]],
) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_ORCH_DDL)
    for run_id, status, finished_at in runs:
        conn.execute(
            "INSERT INTO orchestrator_run (id, kind, status, started_at, finished_at) "
            "VALUES (?, 'pipeline', ?, '2026-10-02T00:00:00Z', ?)",
            (run_id, status, finished_at),
        )
    for ordinal, (run_id, step, status) in enumerate(steps, start=1):
        conn.execute(
            "INSERT INTO orchestrator_step (orchestrator_run_id, ordinal, step, argv_json, status) "
            "VALUES (?, ?, ?, '[]', ?)",
            (run_id, ordinal, step, status),
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


def test_verify_pilot_t015_passes_a_reconciled_run(tmp_path: Path) -> None:
    _write_orch_db(
        tmp_path / "fin.db",
        runs=[(1, "abandoned", "2026-10-02T00:01:00Z"), (2, "running", None)],
        steps=[
            (1, "pricing", "abandoned"),
            (1, "fundamental", "not_run"),
            (2, "pricing", "running"),
        ],
    )
    assert "[PASS] T-015" in _verify_pilot_line(tmp_path / "fin.db", "T-015")


def test_verify_pilot_t015_fails_two_running_runs(tmp_path: Path) -> None:
    _write_orch_db(
        tmp_path / "fin.db",
        runs=[(1, "running", None), (2, "running", None)],
        steps=[(1, "pricing", "running"), (2, "pricing", "running")],
    )
    line = _verify_pilot_line(tmp_path / "fin.db", "T-015")
    assert "[FAIL] T-015" in line
    assert "2 runs are running at once" in line


def test_verify_pilot_t015_fails_an_abandoned_run_with_a_live_step(tmp_path: Path) -> None:
    _write_orch_db(
        tmp_path / "fin.db",
        runs=[(1, "abandoned", "2026-10-02T00:01:00Z")],
        steps=[(1, "pricing", "running")],
    )
    line = _verify_pilot_line(tmp_path / "fin.db", "T-015")
    assert "[FAIL] T-015" in line
    assert "run 1: abandoned but step pricing is running" in line


def test_verify_pilot_t015_fails_an_abandoned_step_under_a_live_run(tmp_path: Path) -> None:
    _write_orch_db(
        tmp_path / "fin.db",
        runs=[(1, "completed", "2026-10-02T00:01:00Z")],
        steps=[(1, "pricing", "abandoned")],
    )
    line = _verify_pilot_line(tmp_path / "fin.db", "T-015")
    assert "[FAIL] T-015" in line
    assert "step pricing of run 1: abandoned but its run is not" in line
