"""The cross-module orchestrator (T-011): a step table, a runner, and a CLI wrapper.

Sequencing ``pricing_agent`` -> ``fundamental_agent`` -> ``entity_resolution`` ->
``cycle`` -> ``quant`` for one ``--analysis-date``, as the run endpoints of
``api/routers/run.py`` and the ``python -m api orchestrate`` CLI both call.

Every step runs as a subprocess built from fixed pieces: ``[sys.executable, "-m",
<package>, <command>, <literal flags...>]``. The package, the command and every flag
name are literals in :data:`_PIPELINE`; the only values that enter are the validated
request fields (an ISO date, an int, two floats, one scheme name) as separate list
items, never concatenated into a command. The preference flags (``--pin``,
``--exclude``, ...), every ``--allow-*`` override, ``--force``/``--fresh`` and the
``migrate``/``backfill``/``undo-run`` commands have no slot in the table or in
:func:`build_argv` -- they cannot be produced, not merely rejected at the request.

The API process never imports a package's run code: every step is a subprocess, so a
step's guards (the clean-tree guard, the stale-price guard, the DQ gate version) stay
exactly as on the CLI. This module is the one part of ``api/`` allowed to open a
write-capable connection to ``KG_FINANCIAL_DB`` (constitution Tech stack #3, FR-014):
it records the run's own provenance (``orchestrator_run`` / ``orchestrator_step``,
T-012) with one short write per state change -- never a transaction held while a child
runs, since the child writes the same file. It also opens read-only connections to read
a step's own run-log row back.

A pipeline run is incremental (T-017): before a step runs, its ``pending`` subcommand -- a
read-only check, built from literals like the step -- says whether there is upstream data the
step has not processed. A step runs when its own check says pending or when a direct upstream
ran because it was pending; otherwise it is ``skipped`` with ``not pending: <evidence>``.
``cycle select`` always runs. A check that fails counts as pending. The single-step kinds never
run a check.

A run that writes takes a cross-process lock (T-015): an exclusive, non-blocking
``fcntl.flock`` on ``<KG_FINANCIAL_DB>.orchestrator.lock``, before it records anything. A run
refused by the lock starts nothing and writes no row; every child inherits the lock's
descriptor, so a step orphaned by a ``kill -9`` still holds it until it exits. Holding the
lock, a run first reconciles any run a dead process left ``running``/``pending`` to
``abandoned`` (its running step ``abandoned``, its pending steps ``not_run``). A graceful stop
-- SIGTERM to the CLI, or the API's shutdown -- kills the current child and records the step
``failed`` with ``stopped: SIGTERM`` / ``stopped: API shutdown`` and the later steps ``not_run``.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import logging
import os
import re
import signal
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from portfolio_common.db import DatabaseError

from api.config import news_database_path
from kg_schema import code_version, connect, connect_ro, ensure
from kg_schema.env import DB_ENV_VAR, database_path, universe_database_path
from kg_schema.rundate import parse_analysis_date

log = logging.getLogger(__name__)

# A copy of ``cycle.construction.SCHEMES``, not an import: importing that package runs
# its ``__init__``, which pulls ``cycle.orchestrator`` (run code) onto ``api/``'s import
# path (T-011 D2). tests/test_orchestrator.py pins the two together.
WEIGHT_SCHEMES = ("score_tilt", "equal", "score_proportional", "inverse_vol")

_FLAG_ANALYSIS_DATE = "--analysis-date"
_FLAG_DB = "--db"
_FLAG_UNIVERSE_DB = "--universe-db"
_FLAG_NEWS_DB = "--news-db"

# Setting name -> its literal flag. The builder only ever looks a *name* up here, so no
# flag outside this table can reach an argv.
_SETTING_FLAG: dict[str, str] = {
    "top_n": "--top-n",
    "weight_scheme": "--weight-scheme",
    "max_name_weight": "--max-name-weight",
    "max_sector_weight": "--max-sector-weight",
}

_OUTPUT_TAIL_LINES = 40

# -- the cross-process run lock (T-015) ----------------------------------------------------------

_LOCK_SUFFIX = ".orchestrator.lock"


@dataclass
class _LockState:
    """The current run's lock and stop state (one active run per process, T-015): the open lock
    fd (inherited by every child through ``pass_fds``), its holder identity, the child currently
    running (so a graceful stop can kill it), and a requested stop's reason."""

    fd: int | None = None
    holder: dict[str, object] | None = None
    child: subprocess.Popen[str] | None = None
    stop_reason: str | None = None


_LOCK_STATE = _LockState()


def lock_path(db_path: Path) -> Path:
    """The run lock file beside the database (``<db>.orchestrator.lock``); never deleted."""
    return db_path.with_name(db_path.name + _LOCK_SUFFIX)


@dataclass(frozen=True)
class RunPaths:
    """The three database paths a run passes to every child (D2, D7)."""

    db_path: Path
    universe_db_path: Path
    news_db_path: Path


@dataclass(frozen=True)
class RunContext:
    """One run's fixed inputs: the paths, the date and the construction settings."""

    paths: RunPaths
    analysis_date: str
    top_n: int | None = None
    weight_scheme: str | None = None
    max_name_weight: float | None = None
    max_sector_weight: float | None = None


@dataclass(frozen=True)
class StepSpec:
    """One subprocess step. ``command``/``flags``/``settings`` are the only pieces; there
    is deliberately no field for a preference or override flag (D2). ``run_kind`` is the
    run-log table this step's own id belongs to (T-012), a literal per step. ``depends_on``
    names the upstream steps whose failure or blockage blocks this one (T-013): direct edges
    only -- blocking is transitive through them."""

    name: str
    package: str
    command: tuple[str, ...]
    run_kind: str
    depends_on: tuple[str, ...] = ()
    flags: tuple[str, ...] = ()
    settings: tuple[str, ...] = ()
    with_news_db: bool = False
    run_id_regex: str | None = None
    quant_command: str | None = None
    # T-017: the read-only check's command (a literal, run as ``python -m <package> <command>
    # --analysis-date ...`` with the step's paths); ``None`` = the step has no check of its own.
    pending_command: tuple[str, ...] | None = None
    # ``cycle select`` runs on every orchestrated run, so it is never skipped, and its run is not
    # a pending signal for the steps after it.
    always_run: bool = False
    note: str | None = None  # appended to the step's reason (a literal)


# Until Work item 4 lands (the ``KG_NLP_DB`` seam) there is no semantic source to check; the run
# says so on the step that would consume it (PLAN, "Incremental, upstream-aware run").
_SEMANTIC_NOTE = "semantic: no semantic source configured"

# A pending check that outlives this is killed and counted as a failed check (= pending). The
# steps themselves have no timeout. 3600 s: the fundamental check's gateway listing took 1530 s for
# 2,046 calls at 503 members on a production copy (T-017 measurement), so 1800 s left 15% headroom.
_CHECK_TIMEOUT_SECONDS = 3600
_MAX_EVIDENCE_CHARS = 300
_PENDING_LINE = re.compile(r"^pending: (yes|no) (.+)$", re.MULTILINE)


# T-013 dependency edges (direct only; blocking is transitive through them). Reasons: cycle
# reads pricing's and fundamental's tables; quant reads prices and the cover-page share count
# fundamental writes (kg_schema.market_cap, PLAN step 4), and ``backfill-actions`` reads its
# asset list from ``assets``, so the quant chain starts after both; each quant command needs
# the previous one's output; ``quant evaluate`` snapshots the live ``portfolio_position`` book
# at ``--from`` (quant/evaluate.py:163, quant/db.py:631), which defaults to the earliest book's
# as-of -- D itself on a fresh database -- so it must not run after a failed select. Nothing
# reads entity_resolution's output (``shared_executive_edge``) in cycle or quant, so nothing
# depends on it.
_PIPELINE: tuple[StepSpec, ...] = (
    StepSpec(
        "pricing",
        "pricing_agent",
        ("run",),
        run_kind="pricing",
        depends_on=(),
        flags=("--store-daily", "--observations"),
        run_id_regex=r"^run (\d+):",
        pending_command=("pending",),
    ),
    StepSpec(
        "fundamental",
        "fundamental_agent",
        ("run",),
        run_kind="analysis",
        depends_on=(),
        run_id_regex=r"^run (\d+):",
        pending_command=("pending",),
    ),
    StepSpec(
        "entity_resolution",
        "entity_resolution",
        ("build",),
        run_kind="cycle",
        depends_on=(),
        with_news_db=True,
        run_id_regex=r"^cycle (\d+):",
        pending_command=("pending",),
    ),
    StepSpec(
        "cycle_select",
        "cycle",
        ("select",),
        run_kind="cycle",
        depends_on=("pricing", "fundamental"),
        settings=("top_n", "weight_scheme", "max_name_weight", "max_sector_weight"),
        run_id_regex=r"^select (\d+)\s",
        always_run=True,
        note=_SEMANTIC_NOTE,
    ),
    StepSpec(
        "quant_actions",
        "quant",
        ("backfill-actions",),
        run_kind="quant",
        depends_on=("pricing", "fundamental"),
        quant_command="backfill-actions",
        pending_command=("pending",),
    ),
    StepSpec(
        "quant_returns",
        "quant",
        ("build-returns",),
        run_kind="quant",
        depends_on=("quant_actions",),
        quant_command="build-returns",
    ),
    StepSpec(
        "quant_risk_model",
        "quant",
        ("build-risk-model",),
        run_kind="quant",
        depends_on=("quant_returns",),
        settings=("top_n", "max_name_weight", "max_sector_weight"),
        quant_command="build-risk-model",
    ),
    StepSpec(
        "quant_optimize",
        "quant",
        ("optimize",),
        run_kind="quant",
        depends_on=("quant_risk_model",),
        settings=("top_n", "max_name_weight", "max_sector_weight"),
        quant_command="optimize",
    ),
    StepSpec(
        "quant_evaluate",
        "quant",
        ("evaluate",),
        run_kind="quant",
        depends_on=("quant_optimize", "cycle_select"),
        quant_command="evaluate",
    ),
)

_MONITOR = StepSpec(
    "cycle_monitor", "cycle", ("monitor",), run_kind="cycle", run_id_regex=r"^monitor (\d+)\s"
)

# Endpoint kind -> the ordered steps it runs.
_PIPELINE_KIND = "pipeline"
_SINGLE_STEPS: dict[str, tuple[StepSpec, ...]] = {
    "pricing-agent": (_PIPELINE[0],),
    "fundamental-agent": (_PIPELINE[1],),
    "entity-resolution": (_PIPELINE[2],),
    "cycle-select": (_PIPELINE[3],),
    "cycle-monitor": (_MONITOR,),
    "quant-backfill-actions": (_PIPELINE[4],),
    "quant-build-returns": (_PIPELINE[5],),
    "quant-build-risk-model": (_PIPELINE[6],),
    "quant-optimize": (_PIPELINE[7],),
    "quant-evaluate": (_PIPELINE[8],),
}


def steps_for(kind: str) -> tuple[StepSpec, ...]:
    """The ordered steps an endpoint/CLI kind runs."""
    if kind == _PIPELINE_KIND:
        return _PIPELINE
    return _SINGLE_STEPS[kind]


# -- argv ---------------------------------------------------------------------------------------


def build_argv(spec: StepSpec, ctx: RunContext) -> list[str]:
    """The exact subprocess argv for *spec*, from literals plus the validated context."""
    argv = [sys.executable, "-m", spec.package, *spec.command]
    argv += list(spec.flags)
    values: dict[str, object] = {
        "top_n": ctx.top_n,
        "weight_scheme": ctx.weight_scheme,
        "max_name_weight": ctx.max_name_weight,
        "max_sector_weight": ctx.max_sector_weight,
    }
    for key in spec.settings:  # fixed order, so an argv is reproducible
        value = values[key]
        if value is not None:
            argv += [_SETTING_FLAG[key], str(value)]
    argv += [
        _FLAG_ANALYSIS_DATE,
        ctx.analysis_date,
        _FLAG_DB,
        str(ctx.paths.db_path),
        _FLAG_UNIVERSE_DB,
        str(ctx.paths.universe_db_path),
    ]
    if spec.with_news_db:
        argv += [_FLAG_NEWS_DB, str(ctx.paths.news_db_path)]
    return argv


def build_check_argv(spec: StepSpec, ctx: RunContext) -> list[str]:
    """The argv of *spec*'s pending check: literals plus the validated context, exactly like the
    step's own (no preference flag, no ``--allow-*``, no ``migrate``)."""
    assert spec.pending_command is not None  # callers test it
    argv = [sys.executable, "-m", spec.package, *spec.pending_command]
    argv += [
        _FLAG_ANALYSIS_DATE,
        ctx.analysis_date,
        _FLAG_DB,
        str(ctx.paths.db_path),
        _FLAG_UNIVERSE_DB,
        str(ctx.paths.universe_db_path),
    ]
    if spec.with_news_db:
        argv += [_FLAG_NEWS_DB, str(ctx.paths.news_db_path)]
    return argv


def _child_env(paths: RunPaths) -> dict[str, str]:
    """The child's environment: the run's three paths, spelled out (D2). Everything else
    (``LLM_*``, gateway URLs) is inherited; no secret is stored or logged."""
    env = dict(os.environ)
    env["KG_FINANCIAL_DB"] = str(paths.db_path)
    env["KG_UNIVERSE_DB"] = str(paths.universe_db_path)
    env["KG_NEWS_DB"] = str(paths.news_db_path)
    return env


@dataclass(frozen=True)
class CommandResult:
    """What :func:`run_command` returns: the exit code and the combined output."""

    returncode: int
    output: str


def run_command(
    argv: list[str], env: dict[str, str], timeout: float | None = None
) -> CommandResult:
    """The single subprocess seam -- tests replace this, never the real agents. No shell,
    no string concatenation: *argv* is a list built by :func:`build_argv` or
    :func:`build_check_argv`. A step has no *timeout*; a pending check does, and the child is
    killed when it expires (``subprocess.TimeoutExpired``).

    ``Popen`` + ``communicate`` instead of ``subprocess.run`` (T-015): the child is registered as
    the current child under the lock *before* it can be asked to stop, so
    :func:`request_stop` always sees it; the lock's fd is inherited through ``pass_fds``. The
    behaviour is otherwise identical: a timeout kills the child and re-raises
    ``TimeoutExpired``; any other ``BaseException`` (a ``KeyboardInterrupt``/``RunStopped``
    raised by a stop) kills the child, exactly as ``subprocess.run``'s bare ``except`` does. A
    stop requested while the child runs makes this raise :class:`RunStopped` -- a stop, never a
    step failure or a "failed check"."""
    with _LOCK:
        stop = _LOCK_STATE.stop_reason
        if stop is not None:  # a stop was requested before this child could start
            raise RunStopped(stop)
        proc = subprocess.Popen(  # noqa: S603 - argv is a list of literals from the step table
            argv,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            pass_fds=(_LOCK_STATE.fd,) if _LOCK_STATE.fd is not None else (),
        )
        _LOCK_STATE.child = proc
    try:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            raise
        except BaseException:
            proc.kill()  # mirror subprocess.run: kill the child, then re-raise
            raise
    finally:
        with _LOCK:
            if _LOCK_STATE.child is proc:
                _LOCK_STATE.child = None
    stop = _stop_reason()
    if stop is not None:
        raise RunStopped(stop)
    return CommandResult(returncode=proc.returncode, output=f"{stdout}{stderr}")


# -- run state ----------------------------------------------------------------------------------


@dataclass
class StepState:
    name: str
    argv: list[str]
    status: str = "pending"  # pending|running|completed|failed|skipped|blocked|not_run|abandoned
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    run_id: int | None = None
    reason: str | None = None
    output_tail: list[str] = field(default_factory=list)
    # provenance (T-012): this step's orchestrator_step row id and the DB it lives in
    pk: int | None = None
    db_path: Path | None = None


@dataclass
class RunState:
    handle: str
    kind: str
    analysis_date: str
    settings: dict[str, object]
    status: str = "pending"  # pending|running|completed|failed|abandoned
    started_at: str | None = None
    finished_at: str | None = None
    steps: list[StepState] = field(default_factory=list)
    # provenance (T-012): the run's orchestrator_run row id, its surface and its DB
    surface: str = "http"  # http|cli
    pk: int | None = None
    db_path: Path | None = None


@dataclass
class _Registry:
    """The process's run state (D4: in memory, lost on restart; one active run)."""

    runs: dict[str, RunState] = field(default_factory=dict)
    active: str | None = None
    thread: threading.Thread | None = None


_REGISTRY = _Registry()
_LOCK = threading.Lock()


class RunInProgress(RuntimeError):
    """A second run was requested while one is active (D4)."""

    def __init__(self, handle: str) -> None:
        super().__init__(f"a run is already in progress: {handle}")
        self.handle = handle


class RunLocked(RuntimeError):
    """Another process -- or a step it started that is still running -- holds the run lock
    (T-015). The HTTP path answers ``409`` with :attr:`holder`; the CLI exits ``1``."""

    MESSAGE = "a previous run, or a step it started, still holds the run lock"

    def __init__(self, holder: dict[str, object] | None) -> None:
        super().__init__(self.MESSAGE)
        self.holder = holder


class RunStopped(BaseException):
    """A graceful stop was requested (T-015: SIGTERM to the CLI, or the API's shutdown).

    A ``BaseException`` so :func:`_execute_step`'s ``except Exception`` cannot swallow it; the
    runner records the current step ``failed`` with :attr:`reason` (or ``not_run`` when the stop
    landed before the step's child started), every later step ``not_run``, and the run ``failed``.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ProvenanceError(RuntimeError):
    """The run's provenance row could not be written, so the run does not start (T-012).
    The HTTP path answers ``500``; the CLI exits ``1``."""


class _ProvenanceWriteFailed(RuntimeError):
    """A provenance write failed after the run started (T-012): stop the run -- no further
    child starts -- and never leave the thread dead with the row still ``running``."""

    def __init__(self, original: BaseException) -> None:
        super().__init__(f"provenance write failed: {type(original).__name__}: {original}")
        self.original = original


def get_run(handle: str) -> RunState | None:
    with _LOCK:
        return _REGISTRY.runs.get(handle)


def clear_runs() -> None:
    """Test seam: drop every in-memory run (the D4 state is lost on restart) and release the
    run lock (T-015)."""
    with _LOCK:
        _REGISTRY.runs.clear()
        _REGISTRY.active = None
        _REGISTRY.thread = None
        _LOCK_STATE.stop_reason = None
    _release_lock()


# -- the run lock (T-015) ------------------------------------------------------------------------


def _read_holder(fd: int) -> dict[str, object] | None:
    """The holder identity written into the lock file, or ``None`` if it is absent/unreadable."""
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 64 * 1024)
    except OSError:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _acquire_lock(db_path: Path, holder: dict[str, object]) -> None:
    """Take the exclusive run lock, or raise :class:`RunLocked` with the current holder.

    Non-blocking: a run refused here starts nothing. The holder identity is written into the
    file *after* acquiring, so a reader sees the run that actually holds it."""
    fd = os.open(lock_path(db_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        data = _read_holder(fd)
        os.close(fd)
        raise RunLocked(data) from exc
    payload = json.dumps(holder).encode("utf-8")
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    os.write(fd, payload)
    _LOCK_STATE.fd = fd
    _LOCK_STATE.holder = holder


def _release_lock() -> None:
    """Close the lock fd (releasing the ``flock``); idempotent."""
    fd, _LOCK_STATE.fd = _LOCK_STATE.fd, None
    _LOCK_STATE.holder = None
    if fd is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def _stop_reason() -> str | None:
    with _LOCK:
        return _LOCK_STATE.stop_reason


def request_stop(reason: str) -> None:
    """Ask the current run to stop (T-015), atomically with the child registration.

    Under the lock: set the reason -- so :func:`run_command` refuses to spawn any further child --
    and kill the child currently registered, so a stop during a pending check or a step is a stop,
    never a failed check or a step failure."""
    with _LOCK:
        _LOCK_STATE.stop_reason = reason
        child = _LOCK_STATE.child
        if child is not None and child.poll() is None:
            child.kill()


def shutdown_active_run(timeout: float = 10.0) -> None:
    """The API lifespan's shutdown hook (T-015): stop the active run and wait (bounded) for the
    runner to record it, so the run is ``failed``/``stopped: API shutdown`` before the process
    exits. If the runner does not finish in time, the next run reconciles it (T-015)."""
    with _LOCK:
        active = _REGISTRY.active
        thread = _REGISTRY.thread
    if active is None:
        return
    request_stop("stopped: API shutdown")
    if thread is not None:
        thread.join(timeout)


def _now() -> str:
    return datetime.now(tz=UTC).isoformat(timespec="seconds")


def _tail(output: str) -> list[str]:
    return output.splitlines()[-_OUTPUT_TAIL_LINES:]


def _provided_settings(ctx: RunContext) -> dict[str, object]:
    pairs: dict[str, object] = {
        "top_n": ctx.top_n,
        "weight_scheme": ctx.weight_scheme,
        "max_name_weight": ctx.max_name_weight,
        "max_sector_weight": ctx.max_sector_weight,
    }
    return {key: value for key, value in pairs.items() if value is not None}


# -- provenance (T-012): one short write per state change ----------------------------------------

# Static literal SQL (Code & Git #10): no identifier is ever built at runtime; every value is a
# bound ? parameter. Each write opens, commits and closes its own connection, so no transaction
# is held while a child runs (the child writes the same file).
_RUN_INSERT_SQL = (
    "INSERT INTO orchestrator_run "
    "(handle, kind, as_of, surface, settings_json, status, started_at, code_version) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)
_STEP_INSERT_SQL = (
    "INSERT INTO orchestrator_step "
    "(orchestrator_run_id, ordinal, step, argv_json, status, run_kind) "
    "VALUES (?, ?, ?, ?, 'pending', ?)"
)
_RUN_UPDATE_SQL = (
    "UPDATE orchestrator_run SET status = ?, started_at = ?, finished_at = ? WHERE id = ?"
)
_STEP_UPDATE_SQL = (
    "UPDATE orchestrator_step SET status = ?, started_at = ?, finished_at = ?, exit_code = ?, "
    "run_id = ?, reason = ?, output_tail = ? WHERE id = ?"
)


def _ensure_tables(db_path: Path) -> None:
    """Additive ``ensure`` (never ``run_migrations``) so a fresh database gets the provenance
    tables and their views before the first write."""
    try:
        conn = connect(db_path)
        try:
            ensure(conn, run_migrations=False)
        finally:
            conn.close()
    except Exception as exc:
        raise ProvenanceError(f"cannot prepare {db_path}: {type(exc).__name__}: {exc}") from exc


def _insert_run(run: RunState, specs: tuple[StepSpec, ...]) -> None:
    """Insert the run row and every step row (status ``pending``) in one short transaction,
    then close -- so a GET right after the 202 finds them."""
    assert run.db_path is not None  # set by new_run before this is called
    try:
        conn = connect(run.db_path)
        try:
            cur = conn.execute(
                _RUN_INSERT_SQL,
                (
                    run.handle,
                    run.kind,
                    run.analysis_date,
                    run.surface,
                    json.dumps(run.settings) if run.settings else None,
                    run.status,
                    run.started_at,
                    code_version(),
                ),
            )
            run_pk = cur.lastrowid
            assert run_pk is not None  # set after an INSERT
            run.pk = int(run_pk)
            for ordinal, (spec, state) in enumerate(zip(specs, run.steps, strict=True), start=1):
                cur = conn.execute(
                    _STEP_INSERT_SQL,
                    (run.pk, ordinal, state.name, json.dumps(state.argv), spec.run_kind),
                )
                step_pk = cur.lastrowid
                assert step_pk is not None  # set after an INSERT
                state.pk = int(step_pk)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        raise ProvenanceError(f"cannot record the run: {type(exc).__name__}: {exc}") from exc


def _persist_run(run: RunState) -> None:
    if run.db_path is None or run.pk is None:
        return
    try:
        conn = connect(run.db_path)
        try:
            conn.execute(_RUN_UPDATE_SQL, (run.status, run.started_at, run.finished_at, run.pk))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        raise _ProvenanceWriteFailed(exc) from exc


def _persist_step(state: StepState) -> None:
    if state.db_path is None or state.pk is None:
        return
    try:
        conn = connect(state.db_path)
        try:
            conn.execute(
                _STEP_UPDATE_SQL,
                (
                    state.status,
                    state.started_at,
                    state.finished_at,
                    state.exit_code,
                    state.run_id,
                    state.reason,
                    json.dumps(state.output_tail),
                    state.pk,
                ),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        raise _ProvenanceWriteFailed(exc) from exc


def new_run(
    kind: str, ctx: RunContext, *, surface: str = "http", handle: str | None = None
) -> RunState:
    """Build a run's initial state (argv precomputed) and record it.

    The ``orchestrator_run`` row and every step row (status ``pending``) are written before
    this returns, so a GET right after the 202 finds them. If the run row cannot be written
    the run does not start: :class:`ProvenanceError` propagates (HTTP ``500`` / CLI exit ``1``).
    """
    specs = steps_for(kind)
    run = RunState(
        handle=handle or str(uuid.uuid4()),
        kind=kind,
        analysis_date=ctx.analysis_date,
        settings=_provided_settings(ctx),
        status="running",
        started_at=_now(),
        steps=[StepState(name=spec.name, argv=build_argv(spec, ctx)[1:]) for spec in specs],
        surface=surface,
        db_path=ctx.paths.db_path,
    )
    for state in run.steps:
        state.db_path = ctx.paths.db_path
    _ensure_tables(ctx.paths.db_path)
    _insert_run(run, specs)
    return run


# -- reconcile abandoned runs, then begin (T-015) ------------------------------------------------

# All static, every value bound. The ids are captured first, then the three UPDATEs are restricted
# to exactly those ids through ``json_each`` (never a timestamp that a concurrent run could share).
_STALE_RUNS_SQL = "SELECT id FROM orchestrator_run WHERE status IN ('running', 'pending')"
_ABANDON_RUNS_SQL = (
    "UPDATE orchestrator_run SET status = 'abandoned', finished_at = ? "
    "WHERE id IN (SELECT value FROM json_each(?))"
)
_ABANDON_STEPS_SQL = (
    "UPDATE orchestrator_step SET status = 'abandoned', finished_at = ?, reason = ? "
    "WHERE status = 'running' AND orchestrator_run_id IN (SELECT value FROM json_each(?))"
)
_NOT_RUN_STEPS_SQL = (
    "UPDATE orchestrator_step SET status = 'not_run' "
    "WHERE status = 'pending' AND orchestrator_run_id IN (SELECT value FROM json_each(?))"
)


def reconcile_abandoned_runs(db_path: Path, *, handle: str) -> tuple[int, int]:
    """Reconcile every run a dead process left ``running``/``pending`` (T-015), under the lock.

    One transaction against the orchestrator's own tables only. Returns ``(runs, steps)``
    reconciled: each run becomes ``abandoned`` with ``finished_at``; its running step becomes
    ``abandoned`` with the reason; its pending steps become ``not_run``. A ``completed``/``failed``
    run is never touched."""
    conn = connect(db_path)
    try:
        ids = [int(row[0]) for row in conn.execute(_STALE_RUNS_SQL).fetchall()]
        if not ids:
            return (0, 0)
        payload = json.dumps(ids)
        now = _now()
        reason = f"the orchestrator process ended while this step ran (reconciled by run {handle})"
        runs = conn.execute(_ABANDON_RUNS_SQL, (now, payload)).rowcount
        abandoned = conn.execute(_ABANDON_STEPS_SQL, (now, reason, payload)).rowcount
        not_run = conn.execute(_NOT_RUN_STEPS_SQL, (payload,)).rowcount
        conn.commit()
    finally:
        conn.close()
    return (runs, abandoned + not_run)


def begin_run(
    kind: str, ctx: RunContext, *, surface: str = "http", handle: str | None = None
) -> tuple[RunState, tuple[int, int]]:
    """Take the run lock, reconcile dead runs, then record the new one (T-015).

    The lock is taken before any row is written, so a refused run (:class:`RunLocked` --
    another process, or an orphaned step it started, holds it) starts nothing. A failure after
    the lock is taken (including :class:`ProvenanceError` from :func:`new_run`) releases it before
    propagating, so one bad run cannot leave every later run refused. Returns the run and the
    ``(runs, steps)`` reconciled."""
    run_handle = handle or str(uuid.uuid4())
    with _LOCK:
        _LOCK_STATE.stop_reason = None  # a fresh run is not stopped by a previous run's stop
    holder: dict[str, object] = {
        "handle": run_handle,
        "pid": os.getpid(),
        "started_at": _now(),
        "surface": surface,
    }
    _acquire_lock(ctx.paths.db_path, holder)
    try:
        _ensure_tables(ctx.paths.db_path)
        reconciled = reconcile_abandoned_runs(ctx.paths.db_path, handle=run_handle)
        run = new_run(kind, ctx, surface=surface, handle=run_handle)
    except BaseException:
        _release_lock()
        raise
    if reconciled[0]:
        log.info("run %s: reconciled %d abandoned run(s), %d step(s)", run_handle, *reconciled)
    return run, reconciled


def _set(state: StepState, **changes: object) -> None:
    """Update a step's in-memory state, then persist it (raises on a write failure)."""
    with _LOCK:
        for key, value in changes.items():
            setattr(state, key, value)
    _persist_step(state)


def _try_set(state: StepState, **changes: object) -> None:
    """Best-effort update for the failure paths: records the in-memory state, logs a write
    failure instead of raising (the run is already being stopped)."""
    with _LOCK:
        for key, value in changes.items():
            setattr(state, key, value)
    try:
        _persist_step(state)
    except _ProvenanceWriteFailed:
        log.exception("orchestrator step %s failed", state.name)


def _mark_not_run(run: RunState, index: int, on_step_change: StepCallback | None) -> None:
    """Record every step from *index* on ``not_run`` (best effort): a stopped run starts nothing."""
    for later in run.steps[index:]:
        _try_set(later, status="not_run")
        _notify(on_step_change, later)


def _fail_run(  # noqa: PLR0913 - the run, the step, its reason, its index and the callback
    run: RunState,
    state: StepState,
    reason: str,
    index: int,
    on_step_change: StepCallback | None,
    *,
    step_ran: bool = True,
) -> None:
    """Record the current step and every later step ``not_run`` (best effort: the write may
    itself be what failed). ``step_ran=False`` (T-015: a stop landed before the step's own child
    started) records the current step ``not_run`` too -- it never ran."""
    if step_ran:
        _try_set(state, status="failed", finished_at=_now(), reason=reason)
    else:
        _try_set(state, status="not_run")
    _notify(on_step_change, state)
    _mark_not_run(run, index + 1, on_step_change)


def _finish_run(run: RunState, failed: bool) -> None:
    """Set the run's final state, clear the active handle and the stop flag, release the run
    lock, and persist (best effort)."""
    with _LOCK:
        run.finished_at = _now()
        run.status = "failed" if failed else "completed"
        if run.handle == _REGISTRY.active:
            _REGISTRY.active = None
            _REGISTRY.thread = None
        _LOCK_STATE.stop_reason = None
    try:
        _persist_run(run)
    except _ProvenanceWriteFailed:
        log.exception("orchestrator run %s: final provenance write failed", run.handle)
    _release_lock()


# -- run-id capture -----------------------------------------------------------------------------


def _quant_max_id(db_path: Path, *, command: str | None, after: int) -> int | None:
    """Read ``quant_run`` (read-only, bound values): the newest id, or the newest id of
    *command* greater than *after*. ``None`` when the table/file does not exist yet."""
    if not Path(db_path).exists():
        return None
    conn = connect_ro(db_path)
    try:
        if command is None:
            row = conn.execute("SELECT MAX(id) FROM quant_run").fetchone()
        else:
            row = conn.execute(
                "SELECT MAX(id) FROM quant_run WHERE id > ? AND command = ?",
                (after, command),
            ).fetchone()
    except DatabaseError:
        return None
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    return int(row[0])


def _capture_run_id(
    spec: StepSpec, result: CommandResult, ctx: RunContext, before: int
) -> tuple[int | None, str | None]:
    """This step's own run-log id, or ``None`` plus why when it was expected but missing."""
    if spec.run_id_regex is not None:
        match = re.search(spec.run_id_regex, result.output, re.MULTILINE)
        if match:
            return int(match.group(1)), None
        return None, "run_id not captured"
    if spec.quant_command is not None:
        found = _quant_max_id(ctx.paths.db_path, command=spec.quant_command, after=before)
        if found is not None:
            return found, None
        return None, "run_id not captured"
    return None, None


# -- the runner ---------------------------------------------------------------------------------


def _news_available(path: Path) -> bool:
    return Path(path).exists()


def _run_one(spec: StepSpec, state: StepState, ctx: RunContext) -> None:
    before = (
        _quant_max_id(ctx.paths.db_path, command=None, after=0)
        if spec.quant_command is not None
        else 0
    )
    result = run_command([sys.executable, *state.argv], _child_env(ctx.paths))
    _set(
        state,
        exit_code=result.returncode,
        finished_at=_now(),
        output_tail=_tail(result.output),
    )
    run_id, capture_reason = _capture_run_id(spec, result, ctx, before or 0)
    _set(state, run_id=run_id)
    if result.returncode != 0:
        _set(state, status="failed")
        if capture_reason is not None:
            _set(state, reason=_with_reason(state.reason, capture_reason))
        return
    _set(state, status="completed")
    if capture_reason is not None:
        _set(state, reason=_with_reason(state.reason, capture_reason))


StepCallback = Callable[[StepState], None]


def _notify(on_step_change: StepCallback | None, state: StepState) -> None:
    if on_step_change is not None:
        on_step_change(state)


# A step whose direct upstream ended in one of these is blocked, not run (T-013).
_BLOCKING = ("failed", "blocked")
# Step name -> its position in _PIPELINE, so a blocked reason lists upstreams in step order.
_PIPELINE_ORDER: dict[str, int] = {s.name: i for i, s in enumerate(_PIPELINE)}


def _is_blocked(spec: StepSpec, status_of: dict[str, str]) -> bool:
    """True when any direct upstream of *spec* ended ``failed`` or ``blocked``."""
    return any(status_of.get(name) in _BLOCKING for name in spec.depends_on)


def _blocked_reason(spec: StepSpec, status_of: dict[str, str]) -> str:
    """``blocked by <name> (<status>)[, ...]``: the blocking direct upstreams, in step order."""
    blocking = sorted(
        (name for name in spec.depends_on if status_of.get(name) in _BLOCKING),
        key=_PIPELINE_ORDER.__getitem__,
    )
    return "blocked by " + ", ".join(f"{name} ({status_of[name]})" for name in blocking)


def _with_reason(reason: str | None, extra: str) -> str:
    return f"{reason}; {extra}" if reason else extra


def _short(text: str) -> str:
    return text if len(text) <= _MAX_EVIDENCE_CHARS else text[: _MAX_EVIDENCE_CHARS - 3] + "..."


def _run_check(spec: StepSpec, ctx: RunContext) -> tuple[str, str]:
    """Run *spec*'s pending check: ``("yes"|"no", evidence)``, or ``("failed", cause)`` for a
    non-zero exit, output that is not exactly one ``pending: yes|no <evidence>`` line, or a
    timeout. A failed check is never an error of the run: the caller counts it as pending."""
    try:
        result = run_command(
            build_check_argv(spec, ctx), _child_env(ctx.paths), timeout=_CHECK_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired:
        return "failed", f"timeout after {_CHECK_TIMEOUT_SECONDS} s"
    except Exception as exc:  # the check could not even start
        return "failed", _short(f"{type(exc).__name__}: {exc}")
    if result.returncode != 0:
        last = next((ln for ln in reversed(result.output.splitlines()) if ln.strip()), "")
        return "failed", _short(f"exit {result.returncode}: {last.strip()}")
    lines = _PENDING_LINE.findall(result.output)
    if len(lines) != 1:
        return "failed", f"unparseable output ({len(lines)} pending lines)"
    return lines[0][0], _short(lines[0][1].strip())


@dataclass
class _Progress:
    """What the steps before this one decided, shared down the run."""

    status_of: dict[str, str] = field(default_factory=dict)
    ran_pending: set[str] = field(default_factory=set)  # completed because they were pending
    incremental: bool = True  # False for a single-step kind: no check, no skip


@dataclass(frozen=True)
class _Decision:
    """Whether a step starts, and the reason to record on it (T-017)."""

    run: bool
    reason: str | None
    because_pending: bool = False
    checked: bool = False  # a check ran (so a skip carries its timings)


def _decide(spec: StepSpec, ctx: RunContext, state: StepState, progress: _Progress) -> _Decision:
    """The pending decision of an incremental run: a step runs when a direct upstream ran because
    it was pending, else on its own check (run just before the step, so it sees what the
    upstream steps wrote), else -- it has none -- when it is ``always_run``; otherwise it is
    skipped. A failed check counts as pending."""
    if not progress.incremental:
        return _Decision(run=True, reason=None)
    upstream = sorted(
        (name for name in spec.depends_on if name in progress.ran_pending),
        key=_PIPELINE_ORDER.__getitem__,
    )
    if upstream:
        decision = _Decision(True, "pending upstream: " + ", ".join(upstream), because_pending=True)
    elif spec.pending_command is not None:
        _set(state, started_at=_now())
        verdict, text = _run_check(spec, ctx)
        if verdict == "no":
            return _Decision(False, f"not pending: {text}", checked=True)
        reason = f"pending: {text}" if verdict == "yes" else f"pending check failed: {text}"
        decision = _Decision(True, reason, because_pending=True, checked=True)
    elif spec.always_run:
        decision = _Decision(True, "always runs")
    else:
        ups = ", ".join(
            f"{u} {progress.status_of[u]}" for u in spec.depends_on if u in progress.status_of
        )
        return _Decision(False, f"not pending: no upstream ran on a pending signal ({ups})")
    if spec.note:
        return _Decision(
            True,
            _with_reason(decision.reason, spec.note),
            decision.because_pending,
            decision.checked,
        )
    return decision


def _execute_step(
    spec: StepSpec,
    state: StepState,
    ctx: RunContext,
    progress: _Progress,
    on_step_change: StepCallback | None,
) -> None:
    """Decide and run one step: ``blocked``, ``skipped`` or run. Records ``state.status`` and
    ``progress.status_of``; a failed provenance write or a ``KeyboardInterrupt``/``SystemExit``/
    ``RunStopped`` propagates to :func:`execute_run` (the run stops there).

    The order: blocked by a failed/blocked direct upstream (T-013; no check runs for a step that
    cannot start), the missing-news skip, then (pipeline runs only) :func:`_decide`."""
    status_of = progress.status_of
    if _is_blocked(spec, status_of):
        _set(state, status="blocked", reason=_blocked_reason(spec, status_of))
        _notify(on_step_change, state)
        status_of[spec.name] = "blocked"
        return
    if spec.name == "entity_resolution" and not _news_available(ctx.paths.news_db_path):
        _set(
            state,
            status="skipped",
            reason=(f"no news source configured ({ctx.paths.news_db_path} not found)"),
        )
        _notify(on_step_change, state)
        status_of[spec.name] = "skipped"
        return
    decision = _decide(spec, ctx, state, progress)
    if not decision.run:
        changes: dict[str, object] = {"status": "skipped", "reason": decision.reason}
        if decision.checked:
            changes["finished_at"] = _now()
        _set(state, **changes)
        _notify(on_step_change, state)
        status_of[spec.name] = "skipped"
        return
    _set(state, status="running", started_at=_now(), reason=decision.reason)
    _notify(on_step_change, state)
    try:
        _run_one(spec, state, ctx)
    except _ProvenanceWriteFailed:
        raise  # a failed write stops the run, never an isolated step failure
    except (KeyboardInterrupt, SystemExit, RunStopped):
        raise  # the run stops; recorded by the caller
    except Exception as exc:  # a step that raises must never look completed
        _set(state, status="failed", finished_at=_now(), reason=f"{type(exc).__name__}: {exc}")
    _notify(on_step_change, state)
    status_of[spec.name] = state.status
    # ``cycle select``'s run is never a pending signal for the steps after it (T-017).
    if decision.because_pending and not spec.always_run and state.status == "completed":
        progress.ran_pending.add(spec.name)


def execute_run(
    run: RunState, ctx: RunContext, *, on_step_change: StepCallback | None = None
) -> None:
    """Run *run*'s steps in order, mutating its state.

    A step is ``blocked`` (no child starts) when a direct upstream ended ``failed`` or
    ``blocked``; a missing news source skips only ``entity_resolution`` (D6). A step that
    exits non-zero, or whose run raises an ``Exception``, is ``failed`` and the run continues
    with the next step (T-013). A ``_ProvenanceWriteFailed``, or any ``BaseException`` that is
    not an ``Exception`` (``KeyboardInterrupt``/``SystemExit``/``RunStopped``/...), stops
    the run: the current step is ``failed``, every later step ``not_run``, the run ``failed``,
    and the exception is re-raised when it is not an ``Exception`` -- so the process still
    exits non-zero and the caller can print the run's state. *on_step_change* is called on
    every status transition.

    A stop (T-015) is checked before each step: once :func:`request_stop` has been called, no
    further step starts -- the steps from here on are ``not_run`` and the run is ``failed``. A stop
    that lands while a step runs is recorded on that step as ``failed`` with the stop reason; one
    that lands while its *pending check* runs leaves the step ``not_run`` (it never ran)."""
    specs = steps_for(run.kind)
    progress = _Progress(incremental=run.kind == _PIPELINE_KIND)
    failed = False
    try:
        for index, (spec, state) in enumerate(zip(specs, run.steps, strict=True)):
            if _stop_reason() is not None:
                # A stop arrived between steps: start nothing more (T-015).
                failed = True
                _mark_not_run(run, index, on_step_change)
                break
            try:
                _execute_step(spec, state, ctx, progress, on_step_change)
                if progress.status_of[spec.name] == "failed":
                    failed = True
            except _ProvenanceWriteFailed as exc:
                # A write failed after the run started: stop, no further child runs.
                failed = True
                log.exception("orchestrator run %s failed", run.handle)
                _fail_run(run, state, str(exc), index, on_step_change)
                break
            except BaseException as exc:
                # Any other BaseException (KeyboardInterrupt, SystemExit, RunStopped,
                # GeneratorExit, ...) must not leave the run "completed" with a step still
                # "running": stop it.
                failed = True
                reason = (
                    exc.reason if isinstance(exc, RunStopped) else f"{type(exc).__name__}: {exc}"
                )
                _fail_run(
                    run, state, reason, index, on_step_change, step_ran=state.status == "running"
                )
                if not isinstance(exc, Exception):
                    raise
                break
    finally:
        _finish_run(run, failed)


def run_now(
    kind: str, ctx: RunContext, *, on_step_change: StepCallback | None = None, surface: str = "cli"
) -> RunState:
    """The foreground path (the CLI wrapper): take the lock, reconcile, record and execute in
    this thread (T-015)."""
    run, _ = begin_run(kind, ctx, surface=surface)
    try:
        execute_run(run, ctx, on_step_change=on_step_change)
    finally:
        _release_lock()
    return run


def start_run(kind: str, ctx: RunContext, *, surface: str = "http") -> RunState:
    """The HTTP path: take the lock, reconcile, record, reserve the one active slot and execute
    in a background thread (D4/T-015). A second POST in this process is refused by the in-process
    guard; a run in any other process (or an orphaned step) is refused by the lock."""
    with _LOCK:
        if _REGISTRY.active is not None:
            raise RunInProgress(_REGISTRY.active)
    run, _ = begin_run(kind, ctx, surface=surface)
    with _LOCK:
        _REGISTRY.runs[run.handle] = run
        _REGISTRY.active = run.handle
    thread = threading.Thread(
        target=_run_thread, args=(run, ctx), name=f"orchestrator-{run.handle}", daemon=True
    )
    with _LOCK:
        _REGISTRY.thread = thread
    thread.start()
    return run


def _run_thread(run: RunState, ctx: RunContext) -> None:
    """The HTTP path's background target: a graceful stop is recorded by :func:`execute_run` and
    must not escape the thread as an unhandled exception (T-015)."""
    with contextlib.suppress(RunStopped):
        execute_run(run, ctx)


# -- CLI wrapper --------------------------------------------------------------------------------


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return value


def _weight_cap(text: str) -> float:
    value = float(text)
    if not 0.0 < value <= 1.0:
        raise argparse.ArgumentTypeError("must be in (0, 1]")
    return value


def build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="api orchestrate",
        description="Run the pricing -> fundamental -> entity_resolution -> cycle -> quant "
        "pipeline for one analysis date.",
    )
    parser.add_argument(
        "--analysis-date",
        dest="analysis_date",
        required=True,
        type=parse_analysis_date,
        metavar="YYYY-MM-DD",
    )
    parser.add_argument("--top-n", dest="top_n", type=_positive_int)
    parser.add_argument("--weight-scheme", dest="weight_scheme", choices=WEIGHT_SCHEMES)
    parser.add_argument("--max-name-weight", dest="max_name_weight", type=_weight_cap)
    parser.add_argument("--max-sector-weight", dest="max_sector_weight", type=_weight_cap)
    parser.add_argument("--db", help="override KG_FINANCIAL_DB path")
    parser.add_argument("--universe-db", help="override KG_UNIVERSE_DB path")
    parser.add_argument("--news-db", help="override KG_NEWS_DB (urls.db) path")
    return parser


def _seconds(started_at: str | None, finished_at: str | None) -> str:
    if not started_at or not finished_at:
        return "-"
    delta = datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)
    return f"{delta.total_seconds():.1f}s"


def _result_line(state: StepState) -> str:
    run_id = "-" if state.run_id is None else str(state.run_id)
    line = (
        f"{state.name} {state.status} run_id={run_id} "
        f"{_seconds(state.started_at, state.finished_at)}"
    )
    if state.reason:
        line += f" ({state.reason})"
    return line


def print_step_change(state: StepState) -> None:
    """The CLI wrapper's live line: ``<step> running ...`` when it starts, its result line
    when it ends -- flushed, so a multi-hour run is not silent until it finishes."""
    if state.status == "running":
        print(f"{state.name} running ...", flush=True)
    else:
        print(_result_line(state), flush=True)


def print_run(run: RunState) -> None:
    """The final summary, then every failed step's output tail (T-013: more than one)."""
    counts = {
        status: sum(1 for s in run.steps if s.status == status)
        for status in ("completed", "skipped", "failed", "blocked", "not_run")
    }
    print(
        f"\n{run.kind}: {run.status} "
        f"({counts['completed']} completed, {counts['skipped']} skipped, "
        f"{counts['failed']} failed, {counts['blocked']} blocked, "
        f"{counts['not_run']} not_run, {len(run.steps)} steps)"
    )
    for step in run.steps:
        if step.status != "failed":
            continue
        print(f"{step.name} failed (exit {step.exit_code}); last output lines:")
        for line in step.output_tail:
            print(f"  {line}")


def _on_sigterm(signum: int, frame: object) -> None:
    """The CLI's SIGTERM handler (T-015): raise in the main thread, so ``run_command`` kills the
    current child and the runner records the stop. SIGINT keeps its own behaviour."""
    raise RunStopped("stopped: SIGTERM")


def _install_sigterm() -> None:
    """Install :func:`_on_sigterm` -- main thread only (signal handlers can be set nowhere else)."""
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _on_sigterm)


def cli_main(argv: list[str]) -> int:
    parser = build_cli_parser()
    args = parser.parse_args(argv)
    db = args.db or database_path()
    if not db:
        parser.error(f"no database: pass --db or set {DB_ENV_VAR}")
    ctx = RunContext(
        paths=RunPaths(
            db_path=Path(db).expanduser(),
            universe_db_path=Path(args.universe_db or universe_database_path()).expanduser(),
            news_db_path=Path(news_database_path(args.news_db)).expanduser(),
        ),
        analysis_date=args.analysis_date,
        top_n=args.top_n,
        weight_scheme=args.weight_scheme,
        max_name_weight=args.max_name_weight,
        max_sector_weight=args.max_sector_weight,
    )
    _install_sigterm()
    try:
        run, reconciled = begin_run(_PIPELINE_KIND, ctx, surface="cli")
    except RunLocked as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except ProvenanceError as exc:
        print(f"cannot record the run: {exc}", file=sys.stderr)
        return 1
    if reconciled[0]:
        print(f"reconciled {reconciled[0]} abandoned run(s), {reconciled[1]} step(s)", flush=True)
    try:
        execute_run(run, ctx, on_step_change=print_step_change)
    except (KeyboardInterrupt, SystemExit):
        print_run(run)  # print the state, then let the interrupt exit the process
        raise
    except RunStopped:
        print_run(run)  # a graceful stop: print the state and exit non-zero
        return 1
    finally:
        _release_lock()
    print_run(run)
    return 0 if run.status == "completed" else 1
