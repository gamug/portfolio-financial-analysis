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
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
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
    run-log table this step's own id belongs to (T-012), a literal per step."""

    name: str
    package: str
    command: tuple[str, ...]
    run_kind: str
    flags: tuple[str, ...] = ()
    settings: tuple[str, ...] = ()
    with_news_db: bool = False
    run_id_regex: str | None = None
    quant_command: str | None = None


_PIPELINE: tuple[StepSpec, ...] = (
    StepSpec(
        "pricing",
        "pricing_agent",
        ("run",),
        run_kind="pricing",
        flags=("--store-daily", "--observations"),
        run_id_regex=r"^run (\d+):",
    ),
    StepSpec(
        "fundamental",
        "fundamental_agent",
        ("run",),
        run_kind="analysis",
        run_id_regex=r"^run (\d+):",
    ),
    StepSpec(
        "entity_resolution",
        "entity_resolution",
        ("build",),
        run_kind="cycle",
        with_news_db=True,
        run_id_regex=r"^cycle (\d+):",
    ),
    StepSpec(
        "cycle_select",
        "cycle",
        ("select",),
        run_kind="cycle",
        settings=("top_n", "weight_scheme", "max_name_weight", "max_sector_weight"),
        run_id_regex=r"^select (\d+)\s",
    ),
    StepSpec(
        "quant_actions",
        "quant",
        ("backfill-actions",),
        run_kind="quant",
        quant_command="backfill-actions",
    ),
    StepSpec(
        "quant_returns",
        "quant",
        ("build-returns",),
        run_kind="quant",
        quant_command="build-returns",
    ),
    StepSpec(
        "quant_risk_model",
        "quant",
        ("build-risk-model",),
        run_kind="quant",
        settings=("top_n", "max_name_weight", "max_sector_weight"),
        quant_command="build-risk-model",
    ),
    StepSpec(
        "quant_optimize",
        "quant",
        ("optimize",),
        run_kind="quant",
        settings=("top_n", "max_name_weight", "max_sector_weight"),
        quant_command="optimize",
    ),
    StepSpec(
        "quant_evaluate",
        "quant",
        ("evaluate",),
        run_kind="quant",
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


def run_command(argv: list[str], env: dict[str, str]) -> CommandResult:
    """The single subprocess seam -- tests replace this, never the real agents. No shell,
    no string concatenation: *argv* is a list built by :func:`build_argv`."""
    proc = subprocess.run(  # noqa: S603 - argv is a list of literals from the step table
        argv,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return CommandResult(returncode=proc.returncode, output=f"{proc.stdout}{proc.stderr}")


# -- run state ----------------------------------------------------------------------------------


@dataclass
class StepState:
    name: str
    argv: list[str]
    status: str = "pending"  # pending|running|completed|failed|skipped|not_run
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
    status: str = "pending"  # pending|running|completed|failed
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


_REGISTRY = _Registry()
_LOCK = threading.Lock()


class RunInProgress(RuntimeError):
    """A second run was requested while one is active (D4)."""

    def __init__(self, handle: str) -> None:
        super().__init__(f"a run is already in progress: {handle}")
        self.handle = handle


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
    """Test seam: drop every in-memory run (the D4 state is lost on restart)."""
    with _LOCK:
        _REGISTRY.runs.clear()
        _REGISTRY.active = None


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


def _fail_run(
    run: RunState, state: StepState, reason: str, index: int, on_step_change: StepCallback | None
) -> None:
    """Record a failed step and every later step ``not_run`` (best effort: the write may
    itself be what failed)."""
    _try_set(state, status="failed", finished_at=_now(), reason=reason)
    _notify(on_step_change, state)
    for later in run.steps[index + 1 :]:
        _try_set(later, status="not_run")
        _notify(on_step_change, later)


def _finish_run(run: RunState, failed: bool) -> None:
    """Set the run's final state, clear the active handle, and persist (best effort)."""
    with _LOCK:
        run.finished_at = _now()
        run.status = "failed" if failed else "completed"
        if run.handle == _REGISTRY.active:
            _REGISTRY.active = None
    try:
        _persist_run(run)
    except _ProvenanceWriteFailed:
        log.exception("orchestrator run %s: final provenance write failed", run.handle)


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
            _set(state, reason=capture_reason)
        return
    _set(state, status="completed")
    if capture_reason is not None:
        _set(state, reason=capture_reason)


StepCallback = Callable[[StepState], None]


def _notify(on_step_change: StepCallback | None, state: StepState) -> None:
    if on_step_change is not None:
        on_step_change(state)


def execute_run(
    run: RunState, ctx: RunContext, *, on_step_change: StepCallback | None = None
) -> None:
    """Run *run*'s steps in order, mutating its state. The first step that fails -- a
    non-zero exit (D5), an exception, or a provenance write that fails (T-012) -- ends the
    run; later steps are ``not_run``. A
    missing news source skips only ``entity_resolution`` (D6).

    *on_step_change* is called on every status transition (a step starts, then ends), so
    the CLI wrapper can print a line per step live; the HTTP path passes none. A
    ``KeyboardInterrupt``/``SystemExit`` records the state first, then propagates, so the
    process still exits non-zero and the caller can still print the run's state. A provenance
    write that fails after the run started stops the run (no further child starts), records
    the in-memory state as failed and clears the active handle."""
    specs = steps_for(run.kind)
    failed = False
    try:
        for index, (spec, state) in enumerate(zip(specs, run.steps, strict=True)):
            try:
                if failed:
                    _set(state, status="not_run")
                    _notify(on_step_change, state)
                    continue
                if spec.name == "entity_resolution" and not _news_available(ctx.paths.news_db_path):
                    _set(
                        state,
                        status="skipped",
                        reason=(f"no news source configured ({ctx.paths.news_db_path} not found)"),
                    )
                    _notify(on_step_change, state)
                    continue
                _set(state, status="running", started_at=_now())
                _notify(on_step_change, state)
                _run_one(spec, state, ctx)
                _notify(on_step_change, state)
                if state.status == "failed":
                    failed = True
            except _ProvenanceWriteFailed as exc:
                # A write failed after the run started: stop, no further child runs.
                failed = True
                log.exception("orchestrator run %s failed", run.handle)
                _fail_run(run, state, str(exc), index, on_step_change)
                break
            except BaseException as exc:  # a step that raises must never look completed
                failed = True
                _fail_run(run, state, f"{type(exc).__name__}: {exc}", index, on_step_change)
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
    finally:
        _finish_run(run, failed)


def run_now(
    kind: str, ctx: RunContext, *, on_step_change: StepCallback | None = None, surface: str = "cli"
) -> RunState:
    """The foreground path (the CLI wrapper): build, record and execute in this thread."""
    run = new_run(kind, ctx, surface=surface)
    execute_run(run, ctx, on_step_change=on_step_change)
    return run


def start_run(kind: str, ctx: RunContext, *, surface: str = "http") -> RunState:
    """The HTTP path: record the run, reserve the one active slot and execute it in a
    background thread (D4). The active check and the record happen under the lock, so a 409
    never leaves a run row behind and two POSTs cannot race."""
    with _LOCK:
        if _REGISTRY.active is not None:
            raise RunInProgress(_REGISTRY.active)
        run = new_run(kind, ctx, surface=surface)
        _REGISTRY.runs[run.handle] = run
        _REGISTRY.active = run.handle
    thread = threading.Thread(
        target=execute_run, args=(run, ctx), name=f"orchestrator-{run.handle}", daemon=True
    )
    thread.start()
    return run


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
    """The final summary, and on failure the failing step's output tail."""
    completed = sum(1 for s in run.steps if s.status == "completed")
    skipped = sum(1 for s in run.steps if s.status == "skipped")
    print(
        f"\n{run.kind}: {run.status} "
        f"({completed} completed, {skipped} skipped, {len(run.steps)} steps)"
    )
    failed = next((s for s in run.steps if s.status == "failed"), None)
    if failed is not None:
        print(f"{failed.name} failed (exit {failed.exit_code}); last output lines:")
        for line in failed.output_tail:
            print(f"  {line}")


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
    try:
        run = new_run(_PIPELINE_KIND, ctx, surface="cli")
    except ProvenanceError as exc:
        print(f"cannot record the run: {exc}", file=sys.stderr)
        return 1
    try:
        execute_run(run, ctx, on_step_change=print_step_change)
    except (KeyboardInterrupt, SystemExit):
        print_run(run)  # print the state, then let the interrupt exit the process
        raise
    print_run(run)
    return 0 if run.status == "completed" else 1
